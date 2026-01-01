#!/usr/bin/env python3
"""
Main entry point for the trading bot.
"""

import logging
import sys
import time
import os

# Add the project root to Python path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config.settings import load_config
from src.utils.logger import setup_logging
from src.strategies.strategy_manager import StrategyManager


def main():
    """Main function to run the trading bot."""
    try:
        # Load configuration first
        config = load_config()
        if not config:
            print("ERROR: Failed to load configuration")
            sys.exit(1)
        
        # Setup logging with config
        setup_logging(
            log_level=config.get('logging', {}).get('level', 'INFO'),
            log_file=config.get('logging', {}).get('file', 'trading_bot.log'),
            purge_logs=config.get('logging', {}).get('purge_logs', True),
            max_log_files=config.get('logging', {}).get('max_log_files', 10),
            max_log_age_days=config.get('logging', {}).get('max_log_age_days', 7),
            clear_current_log=config.get('logging', {}).get('clear_current_log', True),
            max_file_size_mb=config.get('logging', {}).get('max_file_size_mb', 50)
        )
        logger = logging.getLogger(__name__)
        
        logger.info("Starting Hyperliquid Trading Bot...")
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
        if 'logger' in locals():
            logger.info("Received interrupt signal, shutting down...")
    except Exception as e:
        if 'logger' in locals():
            logger.error(f"Unexpected error: {e}")
        else:
            print(f"ERROR: {e}")
        sys.exit(1)
    finally:
        if 'strategy_manager' in locals() and 'logger' in locals():
            logger.info("Ensuring proper shutdown...")
            # We force close_positions=True to ensure all trades are closed before exit
            if hasattr(strategy_manager, 'is_running') and strategy_manager.is_running:
                 strategy_manager.stop(close_positions=True)


if __name__ == "__main__":
    main() 