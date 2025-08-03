#!/usr/bin/env python3
"""
Test script to show ALL eligible assets sorted by open interest.
"""

import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from src.config.settings import load_config
from src.api.hyperliquid_sdk_api import HyperliquidSDKAPI

def main():
    """Test asset filtering and show ALL eligible assets."""
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
    print(f"Found {len(universe)} total assets")
    
    # Show filtering criteria
    min_oi = config['trading']['min_open_interest']
    max_oi = config['trading']['max_open_interest']
    print(f"\nFiltering criteria:")
    print(f"Min Open Interest: ${min_oi:,}")
    print(f"Max Open Interest: ${max_oi:,}")
    print(f"Min Volume: $100,000")
    
    # Filter assets manually
    eligible_assets = []
    for asset in universe:
        name = asset.get('name', '')
        oi = float(asset.get('openInterest', 0))
        volume = float(asset.get('volume24h', 0))
        price = float(asset.get('markPrice', 0))
        
        # Check eligibility
        eligible = (
            oi >= min_oi and 
            (max_oi <= 0 or oi <= max_oi) and 
            volume >= 100000 and 
            price > 0
        )
        
        if eligible:
            eligible_assets.append(asset)
    
    # Sort by open interest (descending)
    eligible_assets.sort(key=lambda x: float(x.get('openInterest', 0)), reverse=True)
    
    print(f"\nALL Eligible Assets (sorted by Open Interest):")
    print(f"{'Rank':<4} {'Symbol':<15} {'Open Interest':<15} {'Volume 24h':<15} {'Mark Price':<12}")
    print("-" * 80)
    
    for i, asset in enumerate(eligible_assets):
        name = asset.get('name', 'Unknown')
        oi = float(asset.get('openInterest', 0))
        volume = float(asset.get('volume24h', 0))
        price = float(asset.get('markPrice', 0))
        
        print(f"{i+1:3d}. {name:<15} ${oi:>12,.0f} ${volume:>12,.0f} ${price:>10.4f}")
    
    print(f"\nSummary:")
    print(f"Total assets: {len(universe)}")
    print(f"Eligible assets: {len(eligible_assets)}")
    
    if eligible_assets:
        total_oi = sum(float(asset.get('openInterest', 0)) for asset in eligible_assets)
        avg_oi = total_oi / len(eligible_assets)
        print(f"Average open interest: ${avg_oi:,.0f}")
        print(f"Highest open interest: ${float(eligible_assets[0].get('openInterest', 0)):,.0f}")
        print(f"Lowest open interest: ${float(eligible_assets[-1].get('openInterest', 0)):,.0f}")
    else:
        print("No eligible assets found!")

if __name__ == "__main__":
    main() 