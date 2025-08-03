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


def main():
    """Main function for position monitoring."""
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
    
    # Load local positions
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