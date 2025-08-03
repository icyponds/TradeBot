"""
Logging utilities for the trading bot.
"""

import logging
import logging.handlers
import os
from datetime import datetime


def setup_logging(log_level: str = "INFO", log_file: str = "trading_bot.log"):
    """
    Setup logging configuration for the trading bot.
    
    Args:
        log_level: Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        log_file: Log file path
    """
    # Create logs directory if it doesn't exist
    log_dir = "logs"
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)
    
    # Full path for log file
    log_path = os.path.join(log_dir, log_file)
    
    # Configure logging
    logging.basicConfig(
        level=getattr(logging, log_level.upper()),
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            # Console handler
            logging.StreamHandler(),
            # File handler with rotation
            logging.handlers.RotatingFileHandler(
                log_path,
                maxBytes=10*1024*1024,  # 10MB
                backupCount=5
            )
        ]
    )
    
    # Set specific logger levels
    logging.getLogger('urllib3').setLevel(logging.WARNING)
    logging.getLogger('requests').setLevel(logging.WARNING)
    
    # Log startup message
    logger = logging.getLogger(__name__)
    logger.info("Logging setup complete")


def get_logger(name: str) -> logging.Logger:
    """
    Get a logger instance with the specified name.
    
    Args:
        name: Logger name
        
    Returns:
        Logger instance
    """
    return logging.getLogger(name)


def log_trade(logger: logging.Logger, trade: dict):
    """
    Log a trade with detailed information.
    
    Args:
        logger: Logger instance
        trade: Trade dictionary
    """
    logger.info(
        f"TRADE: {trade['side'].upper()} {trade['symbol']} "
        f"at {trade['price']:.2f} (size: {trade['size']:.2f}) "
        f"via {trade.get('strategy', 'unknown')}"
    )


def log_performance(logger: logging.Logger, performance: dict):
    """
    Log performance metrics.
    
    Args:
        logger: Logger instance
        performance: Performance dictionary
    """
    logger.info("=== PERFORMANCE SUMMARY ===")
    logger.info(f"Total trades: {performance.get('total_trades', 0)}")
    logger.info(f"Open positions: {performance.get('open_positions', 0)}")
    logger.info(f"Total PnL: {performance.get('total_pnl', 0):.2f}")
    
    if 'strategies' in performance:
        logger.info("Strategy Performance:")
        for strategy_name, metrics in performance['strategies'].items():
            logger.info(f"  {strategy_name}:")
            logger.info(f"    Trades: {metrics.get('total_trades', 0)}")
            logger.info(f"    Win Rate: {metrics.get('win_rate', 0):.1f}%")
            logger.info(f"    Avg PnL: {metrics.get('avg_trade_pnl', 0):.2f}")
    
    logger.info("==========================") 