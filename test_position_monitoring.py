#!/usr/bin/env python3
"""
Test script for position monitoring functionality.
This script tests the accuracy of position and order monitoring.
"""

import sys
import os
import json
from datetime import datetime
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from src.config.settings import load_config
from src.api.hyperliquid_sdk_api import HyperliquidSDKAPI
from src.strategies.strategy_manager import StrategyManager


def test_position_sync():
    """Test position synchronization functionality."""
    print("🧪 Testing Position Synchronization")
    print("=" * 50)
    
    # Load configuration
    config = load_config()
    if not config:
        print("ERROR: Failed to load configuration")
        return False
    
    # Initialize strategy manager
    strategy_manager = StrategyManager(config)
    
    # Test position sync
    try:
        strategy_manager.sync_positions_with_exchange()
        print("✅ Position synchronization test passed")
        return True
    except Exception as e:
        print(f"❌ Position synchronization test failed: {e}")
        return False


def test_order_monitoring():
    """Test order monitoring functionality."""
    print("\n🧪 Testing Order Monitoring")
    print("=" * 50)
    
    # Load configuration
    config = load_config()
    if not config:
        print("ERROR: Failed to load configuration")
        return False
    
    # Initialize API
    api = HyperliquidSDKAPI(config)
    
    # Test connection
    if not api.test_connection():
        print("ERROR: Failed to connect to API")
        return False
    
    # Test open orders
    try:
        open_orders = api.get_open_orders()
        print(f"✅ Found {len(open_orders)} open orders")
        
        for order in open_orders:
            print(f"  - {order['symbol']} {order['side']} {order['size']} @ {order['price']}")
        
        return True
    except Exception as e:
        print(f"❌ Order monitoring test failed: {e}")
        return False


def test_position_validation():
    """Test position validation functionality."""
    print("\n🧪 Testing Position Validation")
    print("=" * 50)
    
    # Load configuration
    config = load_config()
    if not config:
        print("ERROR: Failed to load configuration")
        return False
    
    # Initialize strategy manager
    strategy_manager = StrategyManager(config)
    
    # Test position validation
    try:
        validation_results = strategy_manager.validate_position_integrity()
        
        print(f"✅ Position validation completed")
        print(f"  - Total positions: {validation_results['total_positions']}")
        print(f"  - Issues found: {validation_results['total_issues']}")
        print(f"  - Warnings: {validation_results['total_warnings']}")
        print(f"  - Anomalies: {validation_results['total_anomalies']}")
        
        if validation_results['issues']:
            print("  Issues:")
            for issue in validation_results['issues']:
                print(f"    - {issue}")
        
        if validation_results['warnings']:
            print("  Warnings:")
            for warning in validation_results['warnings']:
                print(f"    - {warning}")
        
        return True
    except Exception as e:
        print(f"❌ Position validation test failed: {e}")
        return False


def test_stale_order_cleanup():
    """Test stale order cleanup functionality."""
    print("\n🧪 Testing Stale Order Cleanup")
    print("=" * 50)
    
    # Load configuration
    config = load_config()
    if not config:
        print("ERROR: Failed to load configuration")
        return False
    
    # Initialize strategy manager
    strategy_manager = StrategyManager(config)
    
    # Test stale order cleanup
    try:
        strategy_manager._cleanup_stale_orders()
        print("✅ Stale order cleanup test completed")
        return True
    except Exception as e:
        print(f"❌ Stale order cleanup test failed: {e}")
        return False


def main():
    """Run all position monitoring tests."""
    print("🔍 POSITION MONITORING TEST SUITE")
    print("=" * 60)
    print(f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print()
    
    tests = [
        ("Position Synchronization", test_position_sync),
        ("Order Monitoring", test_order_monitoring),
        ("Position Validation", test_position_validation),
        ("Stale Order Cleanup", test_stale_order_cleanup),
    ]
    
    passed = 0
    total = len(tests)
    
    for test_name, test_func in tests:
        try:
            if test_func():
                passed += 1
        except Exception as e:
            print(f"❌ {test_name} test crashed: {e}")
    
    print("\n" + "=" * 60)
    print("📊 TEST RESULTS SUMMARY")
    print("=" * 60)
    print(f"Tests Passed: {passed}/{total}")
    print(f"Success Rate: {(passed/total)*100:.1f}%")
    
    if passed == total:
        print("🎉 All tests passed! Position monitoring is working correctly.")
        return 0
    else:
        print("⚠️  Some tests failed. Please check the configuration and API connection.")
        return 1


if __name__ == "__main__":
    sys.exit(main()) 