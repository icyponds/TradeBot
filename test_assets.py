#!/usr/bin/env python3
"""
Test script to examine available assets and their filtering criteria.
"""

import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from src.config.settings import load_config
from src.api.hyperliquid_sdk_api import HyperliquidSDKAPI

def main():
    """Test asset filtering."""
    config = load_config()
    api = HyperliquidSDKAPI(config)
    
    # Test connection
    if not api.test_connection():
        print("Failed to connect to API")
        return
    
    # Get asset info
    asset_info = api.get_asset_info()
    if not asset_info:
        print("Failed to get asset info")
        return
    
    universe = asset_info.get('universe', [])
    print(f"Found {len(universe)} assets")
    
    # Show filtering criteria
    min_oi = config['trading']['min_open_interest']
    max_oi = config['trading']['max_open_interest']
    print(f"\nFiltering criteria:")
    print(f"Min Open Interest: ${min_oi:,}")
    print(f"Max Open Interest: ${max_oi:,}")
    print(f"Min Volume: $100,000")
    
    # Analyze assets
    eligible_count = 0
    total_oi = 0
    
    print(f"\nAsset Analysis:")
    print(f"{'Symbol':<15} {'Open Interest':<15} {'Volume 24h':<15} {'Mark Price':<12} {'Eligible':<10}")
    print("-" * 80)
    
    for asset in universe[:20]:  # Show first 20 assets
        name = asset.get('name', 'Unknown')
        oi = float(asset.get('openInterest', 0))
        volume = float(asset.get('volume24h', 0))
        price = float(asset.get('markPrice', 0))
        
        # Check eligibility
        eligible = (
            oi >= min_oi and 
            oi <= max_oi and 
            volume >= 100000 and 
            price > 0
        )
        
        if eligible:
            eligible_count += 1
        
        total_oi += oi
        
        print(f"{name:<15} ${oi:>12,.0f} ${volume:>12,.0f} ${price:>10.4f} {'✓' if eligible else '✗'}")
    
    print(f"\nSummary:")
    print(f"Total assets: {len(universe)}")
    print(f"Eligible assets: {eligible_count}")
    print(f"Average open interest: ${total_oi/len(universe):,.0f}")
    
    # Show some assets with highest open interest
    print(f"\nTop 10 assets by open interest:")
    sorted_assets = sorted(universe, key=lambda x: float(x.get('openInterest', 0)), reverse=True)
    for i, asset in enumerate(sorted_assets[:10]):
        name = asset.get('name', 'Unknown')
        oi = float(asset.get('openInterest', 0))
        volume = float(asset.get('volume24h', 0))
        print(f"{i+1:2d}. {name:<15} OI: ${oi:>12,.0f} Volume: ${volume:>12,.0f}")

if __name__ == "__main__":
    main() 