#!/usr/bin/env python3

import os
import sys
import logging
from dotenv import load_dotenv

# Add src to path
sys.path.append('src')

from src.api.hyperliquid_sdk_api import HyperliquidSDKAPI
from src.config.settings import load_config

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

def test_account_balance():
    """Test account balance retrieval."""
    print("Testing Hyperliquid account balance retrieval...")
    
    # Load configuration
    load_dotenv()
    config = load_config()
    
    # Initialize API
    api = HyperliquidSDKAPI(config)
    
    # Test connection
    print(f"Testing connection to {config['api']['base_url']}...")
    if api.test_connection():
        print("✅ Connection successful")
    else:
        print("❌ Connection failed")
        return
    
    # Get account balance
    print("\nRetrieving account balance...")
    balance = api.get_account_balance()
    
    if balance:
        print(f"✅ Account balance retrieved:")
        print(f"   Public Account: {balance['wallet_address']}")
        print(f"   Total Equity: ${balance['total_equity']:.2f}")
        print(f"   Free Margin: ${balance['free_margin']:.2f}")
        print(f"   Used Margin: ${balance['used_margin']:.2f}")
        print(f"   Unrealized PnL: ${balance['unrealized_pnl']:.2f}")
        print(f"   Realized PnL: ${balance['realized_pnl']:.2f}")
    else:
        print("❌ Failed to retrieve account balance")

if __name__ == "__main__":
    test_account_balance() 