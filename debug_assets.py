#!/usr/bin/env python3
"""
Debug script to check asset data.
"""

import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from src.config.settings import load_config
from src.api.hyperliquid_sdk_api import HyperliquidSDKAPI

def main():
    """Debug asset data."""
    config = load_config()
    api = HyperliquidSDKAPI(config)
    
    print(f"Config min_open_interest: {config['trading']['min_open_interest']}")
    print(f"Config max_open_interest: {config['trading']['max_open_interest']}")
    
    # Get asset info
    asset_info = api.get_asset_info()
    if not asset_info:
        print("Failed to get asset info")
        return
    
    universe = asset_info.get('universe', [])
    print(f"Found {len(universe)} assets")
    
    # Check first 10 assets
    for i, asset in enumerate(universe[:10]):
        name = asset.get('name', 'Unknown')
        oi = float(asset.get('openInterest', 0))
        volume = float(asset.get('volume24h', 0))
        price = float(asset.get('markPrice', 0))
        
        print(f"{i+1}. {name}: OI=${oi:,.0f}, Vol=${volume:,.0f}, Price=${price:.4f}")
        
        # Check filtering
        min_oi = config['trading']['min_open_interest']
        max_oi = config['trading']['max_open_interest']
        
        eligible = (
            oi >= min_oi and 
            (max_oi <= 0 or oi <= max_oi) and 
            volume >= 100000 and 
            price > 0
        )
        
        print(f"   Eligible: {eligible} (OI >= {min_oi}, OI <= {max_oi if max_oi > 0 else 'unlimited'}, Vol >= 100k, Price > 0)")

if __name__ == "__main__":
    main() 