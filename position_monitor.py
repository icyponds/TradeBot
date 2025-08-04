#!/usr/bin/env python3
"""
Position Monitor Script
This script provides comprehensive monitoring and validation of active positions and orders.

Optimized for scalping with:
- 30-second order timeouts
- 10-second position sync intervals
- Real-time validation and cleanup
"""

import sys
import os
import json
from datetime import datetime
from typing import Dict, List, Any
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from src.config.settings import load_config
from src.api.hyperliquid_sdk_api import HyperliquidSDKAPI
from src.models.trade import Position


def load_positions_from_file() -> Dict[str, Any]:
    """Load positions from JSON file."""
    try:
        with open('positions.json', 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def validate_position_accuracy(api: HyperliquidSDKAPI, positions_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Validate position accuracy by comparing local data with exchange data.
    
    Args:
        api: API client instance
        positions_data: Local positions data
        
    Returns:
        Validation results dictionary
    """
    validation_results = {
        'total_local_positions': len(positions_data),
        'total_exchange_positions': 0,
        'matching_positions': 0,
        'missing_on_exchange': 0,
        'missing_locally': 0,
        'size_discrepancies': 0,
        'price_discrepancies': 0,
        'issues': [],
        'warnings': [],
        'details': []
    }
    
    try:
        # Get exchange positions
        exchange_positions = api.get_positions()
        validation_results['total_exchange_positions'] = len(exchange_positions)
        
        # Create lookup dictionaries
        exchange_positions_dict = {pos['symbol']: pos for pos in exchange_positions}
        local_symbols = set(positions_data.keys())
        exchange_symbols = set(exchange_positions_dict.keys())
        
        # Check for positions that exist locally but not on exchange
        missing_on_exchange = local_symbols - exchange_symbols
        for symbol in missing_on_exchange:
            validation_results['missing_on_exchange'] += 1
            validation_results['issues'].append(f"Position {symbol} exists locally but not on exchange")
        
        # Check for positions that exist on exchange but not locally
        missing_locally = exchange_symbols - local_symbols
        for symbol in missing_locally:
            validation_results['missing_locally'] += 1
            validation_results['warnings'].append(f"Position {symbol} exists on exchange but not tracked locally")
        
        # Validate matching positions
        for symbol, local_data in positions_data.items():
            if symbol not in exchange_positions_dict:
                continue
                
            exchange_data = exchange_positions_dict[symbol]
            validation_results['matching_positions'] += 1
            
            # Check size discrepancies
            local_size = local_data['size']
            exchange_size = abs(exchange_data['size'])
            size_diff = abs(local_size - exchange_size)
            
            if size_diff > 0.01:  # Allow for small floating point differences
                validation_results['size_discrepancies'] += 1
                validation_results['warnings'].append(
                    f"Size discrepancy for {symbol}: local={local_size}, exchange={exchange_size}"
                )
            
            # Check price discrepancies
            current_price = api.get_current_price(symbol)
            if current_price and exchange_data['mark_price']:
                price_diff_pct = abs(current_price - exchange_data['mark_price']) / exchange_data['mark_price'] * 100
                if price_diff_pct > 1.0:  # More than 1% difference
                    validation_results['price_discrepancies'] += 1
                    validation_results['warnings'].append(
                        f"Price discrepancy for {symbol}: local=${current_price}, exchange=${exchange_data['mark_price']} ({price_diff_pct:.2f}%)"
                    )
            
            # Add detailed comparison
            validation_results['details'].append({
                'symbol': symbol,
                'local_size': local_size,
                'exchange_size': exchange_size,
                'size_diff': size_diff,
                'local_price': current_price,
                'exchange_price': exchange_data['mark_price'],
                'entry_price': local_data['entry_price'],
                'side': local_data['side'],
                'strategy': local_data['strategy']
            })
        
        return validation_results
        
    except Exception as e:
        validation_results['issues'].append(f"Validation error: {e}")
        return validation_results


def check_open_orders(api: HyperliquidSDKAPI) -> Dict[str, Any]:
    """
    Check and validate open orders.
    
    Args:
        api: API client instance
        
    Returns:
        Order validation results
    """
    order_results = {
        'total_orders': 0,
        'orders': [],
        'issues': []
    }
    
    try:
        open_orders = api.get_open_orders()
        order_results['total_orders'] = len(open_orders)
        
        for order in open_orders:
            order_results['orders'].append({
                'symbol': order['symbol'],
                'side': order['side'],
                'size': order['size'],
                'price': order['price'],
                'order_id': order['order_id'],
                'order_type': order.get('order_type', 'limit')
            })
            
            # Check for potentially problematic orders
            if order['size'] <= 0:
                order_results['issues'].append(f"Invalid order size for {order['symbol']}: {order['size']}")
            
            if order['price'] <= 0:
                order_results['issues'].append(f"Invalid order price for {order['symbol']}: {order['price']}")
        
        return order_results
        
    except Exception as e:
        order_results['issues'].append(f"Error checking orders: {e}")
        return order_results


def display_validation_report(validation_results: Dict[str, Any], order_results: Dict[str, Any]):
    """Display comprehensive validation report."""
    print("=" * 80)
    print("📊 POSITION & ORDER VALIDATION REPORT")
    print("=" * 80)
    print(f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()
    
    # Position Summary
    print("📈 POSITION SUMMARY")
    print("-" * 40)
    print(f"Local Positions: {validation_results['total_local_positions']}")
    print(f"Exchange Positions: {validation_results['total_exchange_positions']}")
    print(f"Matching Positions: {validation_results['matching_positions']}")
    print(f"Missing on Exchange: {validation_results['missing_on_exchange']}")
    print(f"Missing Locally: {validation_results['missing_locally']}")
    print(f"Size Discrepancies: {validation_results['size_discrepancies']}")
    print(f"Price Discrepancies: {validation_results['price_discrepancies']}")
    print()
    
    # Order Summary
    print("📋 ORDER SUMMARY")
    print("-" * 40)
    print(f"Open Orders: {order_results['total_orders']}")
    print()
    
    # Issues and Warnings
    if validation_results['issues'] or order_results['issues']:
        print("🚨 CRITICAL ISSUES")
        print("-" * 40)
        for issue in validation_results['issues'] + order_results['issues']:
            print(f"  ❌ {issue}")
        print()
    
    if validation_results['warnings']:
        print("⚠️  WARNINGS")
        print("-" * 40)
        for warning in validation_results['warnings']:
            print(f"  ⚠️  {warning}")
        print()
    
    # Detailed Position Information
    if validation_results['details']:
        print("📊 DETAILED POSITION COMPARISON")
        print("-" * 80)
        print(f"{'Symbol':<12} {'Side':<5} {'Local Size':<12} {'Exchange Size':<15} {'Size Diff':<10} {'Entry Price':<12} {'Strategy':<15}")
        print("-" * 80)
        
        for detail in validation_results['details']:
            size_diff_str = f"{detail['size_diff']:.4f}" if detail['size_diff'] > 0.001 else "0.0000"
            print(f"{detail['symbol']:<12} {detail['side']:<5} {detail['local_size']:<12.4f} {detail['exchange_size']:<15.4f} {size_diff_str:<10} ${detail['entry_price']:<11.4f} {detail['strategy']:<15}")
        print()
    
    # Open Orders Details
    if order_results['orders']:
        print("📋 OPEN ORDERS DETAILS")
        print("-" * 60)
        print(f"{'Symbol':<12} {'Side':<5} {'Size':<12} {'Price':<12} {'Type':<10}")
        print("-" * 60)
        
        for order in order_results['orders']:
            print(f"{order['symbol']:<12} {order['side']:<5} {order['size']:<12.4f} ${order['price']:<11.4f} {order['order_type']:<10}")
        print()
    
    # Overall Status
    print("🎯 OVERALL STATUS")
    print("-" * 40)
    
    if validation_results['missing_on_exchange'] > 0 or order_results['issues']:
        print("🔴 CRITICAL: Position/order data inconsistencies detected")
    elif validation_results['warnings'] or validation_results['size_discrepancies'] > 0:
        print("🟡 WARNING: Minor discrepancies detected")
    else:
        print("🟢 GOOD: All positions and orders are accurately tracked")
    
    print("=" * 80)


def run_continuous_monitoring(api: HyperliquidSDKAPI, config: Dict[str, Any], args):
    """Run continuous position monitoring."""
    import time
    import signal
    
    # Setup signal handlers for graceful shutdown
    def signal_handler(signum, frame):
        print(f"\n🛑 Received signal {signum}, shutting down gracefully...")
        sys.exit(0)
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Statistics
    total_positions_closed = 0
    emergency_stops_triggered = 0
    last_emergency_check = 0
    
    print("🔄 Starting continuous monitoring loop...")
    print("Press Ctrl+C to stop")
    print()
    
    while True:
        try:
            current_time = time.time()
            
            # Load current positions
            positions_data = load_positions_from_file()
            
            # Validate position accuracy
            validation_results = validate_position_accuracy(api, positions_data)
            
            # Check open orders
            order_results = check_open_orders(api)
            
            # Check for positions that need to be closed
            if args.auto_close:
                positions_to_close = check_positions_for_closure(api, positions_data, args)
                for symbol, reason in positions_to_close:
                    if close_position(api, symbol, reason):
                        total_positions_closed += 1
            
            # Emergency stop check
            if args.emergency_stop:
                if current_time - last_emergency_check >= 30:  # Check every 30 seconds
                    if check_emergency_stop(api, positions_data, args.emergency_threshold):
                        emergency_stops_triggered += 1
                        print("🚨 EMERGENCY STOP TRIGGERED - Closing all positions!")
                        close_all_positions(api, positions_data, "emergency_stop")
                        break
                    last_emergency_check = current_time
            
            # Display status
            display_continuous_status(positions_data, validation_results, order_results, 
                                   total_positions_closed, emergency_stops_triggered)
            
            # Wait for next monitoring cycle
            time.sleep(args.interval)
            
        except KeyboardInterrupt:
            print("\n🛑 Stopping continuous monitoring...")
            break
        except Exception as e:
            print(f"❌ Error in monitoring loop: {e}")
            time.sleep(args.interval)
    
    print("✅ Continuous monitoring stopped")


def check_positions_for_closure(api: HyperliquidSDKAPI, positions_data: Dict[str, Any], args) -> List[tuple]:
    """Check positions and return list of (symbol, reason) for positions that should be closed."""
    positions_to_close = []
    
    for symbol, position_data in positions_data.items():
        current_price = api.get_current_price(symbol)
        if not current_price:
            continue
        
        # Calculate PnL
        entry_price = position_data['entry_price']
        size = position_data['size']
        side = position_data['side']
        
        if side == 'long':
            pnl = (current_price - entry_price) * size
        else:
            pnl = (entry_price - current_price) * size
        
        pnl_percentage = (pnl / (entry_price * size)) * 100
        
        # Check closure criteria
        close_reason = None
        
        # Check stop loss
        if position_data.get('stop_loss'):
            stop_loss = position_data['stop_loss']
            if side == 'long' and current_price <= stop_loss:
                close_reason = "stop_loss"
            elif side == 'short' and current_price >= stop_loss:
                close_reason = "stop_loss"
        
        # Check take profit
        if position_data.get('take_profit'):
            take_profit = position_data['take_profit']
            if side == 'long' and current_price >= take_profit:
                close_reason = "take_profit"
            elif side == 'short' and current_price <= take_profit:
                close_reason = "take_profit"
        
        # Check position timeout
        entry_time = datetime.fromisoformat(position_data['entry_time'])
        time_open = (datetime.now() - entry_time).total_seconds() / 3600  # hours
        if time_open > args.timeout:
            close_reason = "timeout"
        
        # Check loss percentage
        if pnl_percentage < -args.max_loss:
            close_reason = "max_loss"
        
        # Check profit percentage
        if pnl_percentage > args.max_profit:
            close_reason = "max_profit"
        
        if close_reason:
            positions_to_close.append((symbol, close_reason))
    
    return positions_to_close


def close_position(api: HyperliquidSDKAPI, symbol: str, reason: str) -> bool:
    """Close a position and return True if successful."""
    try:
        # Get position data
        positions_data = load_positions_from_file()
        if symbol not in positions_data:
            print(f"⚠️  No position to close for {symbol}")
            return False
        
        position_data = positions_data[symbol]
        current_price = api.get_current_price(symbol)
        
        if not current_price:
            print(f"❌ Could not get current price for {symbol}")
            return False
        
        # Determine close side
        close_side = 'sell' if position_data['side'] == 'long' else 'buy'
        
        # Place close order
        order_result = api.place_order(symbol, close_side, position_data['size'], current_price)
        
        if order_result and order_result.get('status') in ['success', 'pending']:
            print(f"✅ Closed position {symbol} due to {reason}: {close_side} {position_data['size']} @ {current_price}")
            return True
        else:
            print(f"❌ Failed to close position for {symbol}")
            return False
            
    except Exception as e:
        print(f"❌ Error closing position for {symbol}: {e}")
        return False


def close_all_positions(api: HyperliquidSDKAPI, positions_data: Dict[str, Any], reason: str):
    """Close all positions."""
    print(f"🔄 Closing all positions due to {reason}...")
    
    symbols_to_close = list(positions_data.keys())
    closed_count = 0
    
    for symbol in symbols_to_close:
        if close_position(api, symbol, reason):
            closed_count += 1
            time.sleep(0.5)  # Small delay between orders
    
    print(f"✅ Emergency stop complete: closed {closed_count}/{len(symbols_to_close)} positions")


def check_emergency_stop(api: HyperliquidSDKAPI, positions_data: Dict[str, Any], threshold: float) -> bool:
    """Check if emergency stop should be triggered."""
    try:
        total_loss = 0.0
        total_capital_at_risk = 0.0
        
        for symbol, position_data in positions_data.items():
            current_price = api.get_current_price(symbol)
            if not current_price:
                continue
            
            entry_price = position_data['entry_price']
            size = position_data['size']
            side = position_data['side']
            
            # Calculate PnL
            if side == 'long':
                pnl = (current_price - entry_price) * size
            else:
                pnl = (entry_price - current_price) * size
            
            # Calculate capital at risk
            capital_at_risk = entry_price * size
            
            total_loss += pnl
            total_capital_at_risk += capital_at_risk
        
        if total_capital_at_risk > 0:
            portfolio_loss_percentage = (total_loss / total_capital_at_risk) * 100
            
            if portfolio_loss_percentage < -threshold:
                print(f"🚨 EMERGENCY STOP: Portfolio loss {portfolio_loss_percentage:.2f}% exceeds threshold {threshold}%")
                return True
        
        return False
        
    except Exception as e:
        print(f"❌ Error checking emergency stop: {e}")
        return False


def display_continuous_status(positions_data: Dict[str, Any], validation_results: Dict[str, Any], 
                           order_results: Dict[str, Any], total_closed: int, emergency_stops: int):
    """Display current monitoring status."""
    print("=" * 80)
    print("📊 CONTINUOUS POSITION MONITOR STATUS")
    print("=" * 80)
    print(f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Active Positions: {len(positions_data)}")
    print(f"Positions Closed: {total_closed}")
    print(f"Emergency Stops: {emergency_stops}")
    print()
    
    if positions_data:
        total_unrealized_pnl = 0.0
        total_capital_at_risk = 0.0
        
        for symbol, position_data in positions_data.items():
            current_price = api.get_current_price(symbol)
            if not current_price:
                continue
            
            entry_price = position_data['entry_price']
            size = position_data['size']
            side = position_data['side']
            
            # Calculate PnL
            if side == 'long':
                pnl = (current_price - entry_price) * size
            else:
                pnl = (entry_price - current_price) * size
            
            pnl_percentage = (pnl / (entry_price * size)) * 100
            capital_at_risk = entry_price * size
            
            total_unrealized_pnl += pnl
            total_capital_at_risk += capital_at_risk
            
            # Calculate time open
            entry_time = datetime.fromisoformat(position_data['entry_time'])
            time_open = (datetime.now() - entry_time).total_seconds() / 3600  # hours
            
            # Determine status
            if pnl > 0:
                status = "🟢 PROFIT"
            elif pnl < 0:
                status = "🔴 LOSS"
            else:
                status = "⚪ BREAKEVEN"
            
            print(f"{symbol:<12} {side:<5} {size:>8.3f} @ ${entry_price:<8.4f} → ${current_price:<8.4f} | PnL: ${pnl:>8.2f} ({pnl_percentage:>6.2f}%) | Time: {time_open:>4.1f}h | {status}")
        
        if total_capital_at_risk > 0:
            portfolio_pnl_percentage = (total_unrealized_pnl / total_capital_at_risk) * 100
            print("-" * 80)
            print(f"TOTAL: {len(positions_data)} positions | PnL: ${total_unrealized_pnl:>8.2f} ({portfolio_pnl_percentage:>6.2f}%) | Capital at Risk: ${total_capital_at_risk:>8.2f}")
    
    print("=" * 80)


def main():
    """Main function for position monitoring."""
    import argparse
    
    parser = argparse.ArgumentParser(description='Position Monitor')
    parser.add_argument('--continuous', '-c', action='store_true', 
                       help='Run in continuous monitoring mode')
    parser.add_argument('--interval', '-i', type=int, default=10,
                       help='Monitoring interval in seconds (default: 10)')
    parser.add_argument('--timeout', '-t', type=float, default=24.0,
                       help='Position timeout in hours (default: 24)')
    parser.add_argument('--max-loss', type=float, default=5.0,
                       help='Max loss percentage before auto-close (default: 5.0)')
    parser.add_argument('--max-profit', type=float, default=20.0,
                       help='Max profit percentage before auto-close (default: 20.0)')
    parser.add_argument('--emergency-threshold', type=float, default=10.0,
                       help='Emergency stop threshold percentage (default: 10.0)')
    parser.add_argument('--auto-close', action='store_true', default=True,
                       help='Enable automatic position closure (default: True)')
    parser.add_argument('--emergency-stop', action='store_true', default=True,
                       help='Enable emergency stop (default: True)')
    
    args = parser.parse_args()
    
    print("🔍 POSITION & ORDER MONITOR")
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
    
    print("✅ API connection successful")
    
    if args.continuous:
        print(f"🔄 Starting continuous monitoring mode")
        print(f"   Interval: {args.interval} seconds")
        print(f"   Timeout: {args.timeout} hours")
        print(f"   Max Loss: {args.max_loss}%")
        print(f"   Max Profit: {args.max_profit}%")
        print(f"   Emergency Threshold: {args.emergency_threshold}%")
        print(f"   Auto Close: {args.auto_close}")
        print(f"   Emergency Stop: {args.emergency_stop}")
        print()
        
        run_continuous_monitoring(api, config, args)
    else:
        # Run single validation check
        positions_data = load_positions_from_file()
        print(f"📁 Loaded {len(positions_data)} local positions")
        
        # Validate position accuracy
        print("🔍 Validating position accuracy...")
        validation_results = validate_position_accuracy(api, positions_data)
        
        # Check open orders
        print("📋 Checking open orders...")
        order_results = check_open_orders(api)
        
        # Display comprehensive report
        display_validation_report(validation_results, order_results)
        
        # Return appropriate exit code
        if validation_results['missing_on_exchange'] > 0 or order_results['issues']:
            sys.exit(1)  # Critical issues
        elif validation_results['warnings'] or validation_results['size_discrepancies'] > 0:
            sys.exit(2)  # Warnings
        else:
            sys.exit(0)  # All good


if __name__ == "__main__":
    main() 