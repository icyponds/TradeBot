#!/usr/bin/env python3
"""
Main entry point for the trading bot.

SHUTDOWN BEHAVIOR:
- Ctrl+C (SIGINT): Graceful shutdown, closes all positions
- kill <pid> (SIGTERM): Graceful shutdown, closes all positions  
- Terminal closed (SIGHUP): Graceful shutdown, closes all positions
- kill -9 <pid> (SIGKILL): CANNOT BE CAUGHT - positions remain open!
  Use the cleanup script or exchange UI to close positions manually.

For safety, always use Ctrl+C or regular 'kill' to stop the bot.
"""

import logging
import sys
import time
import os
import atexit
import signal

# Add the project root to Python path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config.settings import load_config
from src.utils.logger import setup_logging
from src.strategies.strategy_manager import StrategyManager

# Global reference for cleanup handlers
_strategy_manager = None
_shutdown_in_progress = False


def _cleanup_positions():
    """
    Cleanup handler called on exit.
    Closes all positions to prevent orphaned trades.
    """
    global _strategy_manager, _shutdown_in_progress
    
    if _shutdown_in_progress:
        return  # Avoid double cleanup
    _shutdown_in_progress = True
    
    if _strategy_manager is not None:
        try:
            logger = logging.getLogger(__name__)
            logger.warning("CLEANUP: Closing all positions before exit...")
            
            if hasattr(_strategy_manager, 'is_running') and _strategy_manager.is_running:
                _strategy_manager.stop(close_positions=True)
            elif hasattr(_strategy_manager, 'close_all_positions'):
                # Even if not running, try to close positions
                _strategy_manager.sync_positions_with_exchange()
                _strategy_manager.close_all_positions("emergency_cleanup")
                
            logger.info("CLEANUP: Position cleanup completed")
        except Exception as e:
            print(f"ERROR during cleanup: {e}")


def _signal_handler(signum, frame):
    """
    Handle termination signals gracefully.
    """
    global _shutdown_in_progress
    
    if _shutdown_in_progress:
        return
        
    signal_names = {
        signal.SIGINT: "SIGINT (Ctrl+C)",
        signal.SIGTERM: "SIGTERM (kill)",
        signal.SIGHUP: "SIGHUP (terminal closed)",
    }
    signal_name = signal_names.get(signum, f"Signal {signum}")
    
    logger = logging.getLogger(__name__)
    logger.warning(f"Received {signal_name} - initiating graceful shutdown...")
    
    _cleanup_positions()
    sys.exit(0)


def main():
    """Main function to run the trading bot."""
    global _strategy_manager
    
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
        
        # Register cleanup handlers BEFORE starting
        # atexit handles normal exits, exceptions, and sys.exit()
        atexit.register(_cleanup_positions)
        
        # Signal handlers for interrupt signals
        # Note: SIGKILL (kill -9) CANNOT be caught - this is a kernel limitation
        signal.signal(signal.SIGINT, _signal_handler)
        signal.signal(signal.SIGTERM, _signal_handler)
        signal.signal(signal.SIGHUP, _signal_handler)  # Terminal closed
        
        logger.info("=" * 60)
        logger.info("Starting Hyperliquid Trading Bot...")
        logger.info("SAFETY: All positions will be closed on Ctrl+C, kill, or terminal close")
        logger.info("WARNING: 'kill -9' cannot be caught - use Ctrl+C for safe shutdown!")
        logger.info("=" * 60)
        logger.info("Configuration loaded successfully")
        
        # Initialize strategy manager
        _strategy_manager = StrategyManager(config)
        
        # Test API connection
        logger.info("Testing API connection...")
        if not _strategy_manager.market_api.test_connection():
            logger.error("Failed to connect to Hyperliquid API")
            sys.exit(1)
        
        logger.info("API connection successful")
        
        # Start the strategy manager
        logger.info("Starting strategy manager...")
        _strategy_manager.start()
        
    except KeyboardInterrupt:
        pass  # Handled by signal handler
    except Exception as e:
        logger = logging.getLogger(__name__)
        logger.error(f"Unexpected error: {e}", exc_info=True)
        # Cleanup will be called by atexit
        sys.exit(1)


if __name__ == "__main__":
    main() 