#!/usr/bin/env python3
"""
Debug script to check asset data and filtering.
"""

import os
import sys
import logging
from src.config.settings import load_config

def setup_logging():
    """Setup basic logging."""
    logging.basicConfig(
        level=logging.DEBUG,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

def debug_assets():
    """Debug asset data and filtering."""
    print("=" * 60)
    print("Debugging Asset Data and Filtering")
    print("=" * 60)
    
    # Load configuration
    config = load_config()
    if not config:
        print("ERROR: Failed to load configuration")
        return False
    
    # Initialize API
    from src.api.hyperliquid_sdk_api import HyperliquidSDKAPI
    api = HyperliquidSDKAPI(config)
    
    # Test connection
    print("Testing API connection...")
    if not api.test_connection():
        print("❌ API connection failed")
        return False
    
    print("✅ API connection successful")
    
    # Get asset info
    print("\nFetching asset information...")
    asset_info = api.get_asset_info()
    
    if not asset_info:
        print("❌ Failed to get asset information")
        return False
    
    universe = asset_info.get('universe', [])
    print(f"✅ Retrieved {len(universe)} assets")
    
    # Show first few assets
    print("\nFirst 5 assets:")
    for i, asset in enumerate(universe[:5]):
        print(f"\nAsset {i+1}:")
        print(f"  Name: {asset.get('name', 'N/A')}")
        print(f"  All available fields: {list(asset.keys())}")
        print(f"  Open Interest: {asset.get('openInterest', 'N/A')}")
        print(f"  Volume 24h: {asset.get('volume24h', 'N/A')}")
        print(f"  Mark Price: {asset.get('markPx', 'N/A')}")
        print(f"  Mark Price (alt): {asset.get('markPrice', 'N/A')}")
        print(f"  Bid: {asset.get('bid', 'N/A')}")
        print(f"  Ask: {asset.get('ask', 'N/A')}")
        print(f"  Max Leverage: {asset.get('maxLeverage', 'N/A')}")
        
        # Try to calculate mark price from bid/ask
        bid = float(asset.get('bid', 0))
        ask = float(asset.get('ask', 0))
        if bid > 0 and ask > 0:
            calculated_mark = (bid + ask) / 2
            print(f"  Calculated Mark Price: {calculated_mark:.2f}")
        else:
            print(f"  Calculated Mark Price: N/A (no bid/ask)")
    
    # Check filtering criteria
    min_open_interest = config['trading']['min_open_interest']
    
    print(f"\nFiltering Criteria:")
    print(f"  Min Open Interest: ${min_open_interest:,}")
    
    # Count assets that meet criteria
    eligible_count = 0
    for asset in universe:
        open_interest = float(asset.get('openInterest', 0))
        volume_24h = float(asset.get('volume24h', 0))
        mark_price = float(asset.get('markPrice', 0))
        
        # Check basic criteria (no max open interest limit)
        if open_interest >= min_open_interest and mark_price > 0:
            eligible_count += 1
            if eligible_count <= 10:  # Show first 10 eligible
                print(f"  ✅ {asset.get('name')}: OI=${open_interest:,.0f}, Vol=${volume_24h:,.0f}, Price=${mark_price:.2f}")
    
    print(f"\nSummary:")
    print(f"  Total assets: {len(universe)}")
    print(f"  Eligible assets: {eligible_count}")
    print(f"  Eligible percentage: {(eligible_count/len(universe)*100):.1f}%")
    
    # Test with actual pair selector logic
    print(f"\nTesting with actual pair selector logic:")
    from src.utils.pair_selector import DynamicPairSelector
    from src.api.hyperliquid_sdk_api import HyperliquidSDKAPI
    
    pair_selector = DynamicPairSelector(config, api)
    selected_pairs = pair_selector.scan_and_select_pairs()
    
    print(f"  Selected pairs: {selected_pairs}")
    print(f"  Number of selected pairs: {len(selected_pairs)}")
    
    return True

def main():
    """Main debug function."""
    setup_logging()
    
    if not debug_assets():
        return False
    
    print("\n" + "=" * 60)
    print("✅ Debug completed!")
    print("=" * 60)
    
    return True

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1) 