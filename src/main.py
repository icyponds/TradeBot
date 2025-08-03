#!/usr/bin/env python3
"""
Main entry point for the trading bot.
"""

import logging
import sys
import time
from src.config.settings import load_config
from src.utils.logger import setup_logging
from src.strategies.strategy_manager import StrategyManager


def main():
    """Main function to run the trading bot."""
    try:
        # Setup logging
        setup_logging()
        logger = logging.getLogger(__name__)
        
        logger.info("Starting Hyperliquid Trading Bot...")
        
        # Load configuration
        config = load_config()
        if not config:
            logger.error("Failed to load configuration")
            sys.exit(1)
        
        logger.info("Configuration loaded successfully")
        
        # Initialize strategy manager
        strategy_manager = StrategyManager(config)
        
        # Test API connection
        logger.info("Testing API connection...")
        if not strategy_manager.market_api.test_connection():
            logger.error("Failed to connect to Hyperliquid API")
            sys.exit(1)
        
        logger.info("API connection successful")
        
        # Start the strategy manager
        logger.info("Starting strategy manager...")
        strategy_manager.start()
        
    except KeyboardInterrupt:
        logger.info("Received interrupt signal, shutting down...")
        if 'strategy_manager' in locals():
            strategy_manager.stop()
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main() 