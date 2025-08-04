"""
Logging utilities for the trading bot.
"""

import logging
import logging.handlers
import os
import glob
from datetime import datetime


def purge_old_logs(log_dir: str = "logs", max_log_files: int = 10, max_log_age_days: int = 7, max_file_size_mb: int = 50):
    """
    Purge old log files to prevent disk space issues.
    
    Args:
        log_dir: Directory containing log files
        max_log_files: Maximum number of log files to keep
        max_log_age_days: Maximum age of log files in days
        max_file_size_mb: Maximum file size in MB before truncation
    """
    try:
        if not os.path.exists(log_dir):
            return
        
        # Get all log files
        log_pattern = os.path.join(log_dir, "*.log*")
        log_files = glob.glob(log_pattern)
        
        if not log_files:
            return
        
        # Sort by modification time (oldest first)
        log_files.sort(key=lambda x: os.path.getmtime(x))
        
        # Remove files older than max_log_age_days
        current_time = datetime.now().timestamp()
        max_age_seconds = max_log_age_days * 24 * 3600
        
        for log_file in log_files:
            file_age = current_time - os.path.getmtime(log_file)
            if file_age > max_age_seconds:
                try:
                    os.remove(log_file)
                    print(f"Removed old log file: {log_file}")
                except Exception as e:
                    print(f"Failed to remove old log file {log_file}: {e}")
        
        # Check file sizes and truncate if necessary
        max_file_size_bytes = max_file_size_mb * 1024 * 1024
        for log_file in log_files:
            if os.path.exists(log_file):
                file_size = os.path.getsize(log_file)
                if file_size > max_file_size_bytes:
                    try:
                        # Keep only the last 1000 lines of the file
                        with open(log_file, 'r') as f:
                            lines = f.readlines()
                        
                        # Keep only the last 1000 lines
                        if len(lines) > 1000:
                            lines = lines[-1000:]
                        
                        with open(log_file, 'w') as f:
                            f.writelines(lines)
                        
                        print(f"Truncated large log file: {log_file} (kept last 1000 lines)")
                    except Exception as e:
                        print(f"Failed to truncate log file {log_file}: {e}")
        
        # Keep only the most recent max_log_files
        remaining_files = glob.glob(log_pattern)
        if len(remaining_files) > max_log_files:
            # Sort by modification time (oldest first)
            remaining_files.sort(key=lambda x: os.path.getmtime(x))
            
            # Remove oldest files
            files_to_remove = remaining_files[:-max_log_files]
            for log_file in files_to_remove:
                try:
                    os.remove(log_file)
                    print(f"Removed excess log file: {log_file}")
                except Exception as e:
                    print(f"Failed to remove excess log file {log_file}: {e}")
                    
    except Exception as e:
        print(f"Error purging old logs: {e}")


def setup_logging(log_level: str = "INFO", log_file: str = "trading_bot.log", 
                 purge_logs: bool = True, max_log_files: int = 10, max_log_age_days: int = 7,
                 clear_current_log: bool = True, max_file_size_mb: int = 50):
    """
    Setup logging configuration for the trading bot.
    
    Args:
        log_level: Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        log_file: Log file path
        purge_logs: Whether to purge old logs on startup
        max_log_files: Maximum number of log files to keep
        max_log_age_days: Maximum age of log files in days
        clear_current_log: Whether to clear the current log file on startup
        max_file_size_mb: Maximum file size in MB before truncation
    """
    # Create logs directory if it doesn't exist
    log_dir = "logs"
    if not os.path.exists(log_dir):
        os.makedirs(log_dir)
    
    # Purge old logs if requested
    if purge_logs:
        purge_old_logs(log_dir, max_log_files, max_log_age_days, max_file_size_mb)
    
    # Full path for log file
    log_path = os.path.join(log_dir, log_file)
    
    # Clear current log file if requested
    if clear_current_log and os.path.exists(log_path):
        try:
            # Truncate the file instead of deleting it to preserve file permissions
            with open(log_path, 'w') as f:
                f.write('')
            print(f"Cleared current log file: {log_path}")
        except Exception as e:
            print(f"Failed to clear current log file {log_path}: {e}")
    
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
    
    if purge_logs:
        logger.info(f"Log purging enabled: max {max_log_files} files, max {max_log_age_days} days old")


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