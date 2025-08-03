#!/usr/bin/env python3
"""
Main entry point for the Trading Bot.
"""

import logging
import sys
from pathlib import Path

# Add src to path for imports
sys.path.append(str(Path(__file__).parent))

from config.settings import load_config
from api.hyperliquid_api import HyperliquidAPI
from strategies.strategy_manager import StrategyManager
from utils.logger import setup_logging


def main():
    """Main function to run the trading bot."""
    # Setup logging
    setup_logging()
    logger = logging.getLogger(__name__)
    
    try:
        logger.info("Starting Trading Bot for Hyperliquid...")
        
        # Load configuration
        config = load_config()
        logger.info("Configuration loaded successfully")
        
        # Initialize Hyperliquid API
        market_api = HyperliquidAPI(config)
        logger.info("Hyperliquid API initialized")
        
        # Test connection
        if not market_api.test_connection():
            logger.error("Failed to connect to Hyperliquid API")
            return
        
        # Initialize strategy manager
        strategy_manager = StrategyManager(config, market_api)
        logger.info("Strategy manager initialized")
        
        # Start WebSocket connection for real-time data
        market_api.start_websocket()
        logger.info("WebSocket connection started")
        
        # Start trading
        strategy_manager.run()
        
    except KeyboardInterrupt:
        logger.info("Trading bot stopped by user")
    except Exception as e:
        logger.error(f"Error in main: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main() 