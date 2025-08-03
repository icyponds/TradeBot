"""
Configuration settings for the trading bot.
"""

import os
from typing import Dict, Any
from dotenv import load_dotenv

# Load environment variables
load_dotenv()


def load_config() -> Dict[str, Any]:
    """
    Load configuration from environment variables and defaults.
    
    Returns:
        Dict containing configuration settings
    """
    config = {
        # Hyperliquid API Configuration
        "api": {
            "base_url": os.getenv("HYPERLIQUID_API_URL", "https://api.hyperliquid.xyz"),
            "ws_url": os.getenv("HYPERLIQUID_WS_URL", "wss://api.hyperliquid.xyz/ws"),
            "private_key": os.getenv("HYPERLIQUID_PRIVATE_KEY", ""),
            "wallet_address": os.getenv("HYPERLIQUID_WALLET_ADDRESS", ""),
            "timeout": int(os.getenv("API_TIMEOUT", "30")),
        },
        
        # Trading Configuration
        "trading": {
            "dynamic_pair_selection": os.getenv("DYNAMIC_PAIR_SELECTION", "true").lower() == "true",
            "min_open_interest": float(os.getenv("MIN_OPEN_INTEREST", "1000000")),
            "max_open_interest": float(os.getenv("MAX_OPEN_INTEREST", "100000000")),
            "max_pairs_to_trade": int(os.getenv("MAX_PAIRS_TO_TRADE", "10")),
            "scan_interval_minutes": int(os.getenv("SCAN_INTERVAL_MINUTES", "60")),
            "excluded_assets": [asset.strip() for asset in os.getenv("EXCLUDED_ASSETS", "").split(",") if asset.strip()],
            "included_assets": [asset.strip() for asset in os.getenv("INCLUDED_ASSETS", "").split(",") if asset.strip()],
            "base_currency": os.getenv("BASE_CURRENCY", "USDC"),
            "max_position_size": float(os.getenv("MAX_POSITION_SIZE", "50")),
            "risk_percentage": float(os.getenv("RISK_PERCENTAGE", "2.0")),
            "stop_loss_percentage": float(os.getenv("STOP_LOSS_PERCENTAGE", "5.0")),
            "take_profit_percentage": float(os.getenv("TAKE_PROFIT_PERCENTAGE", "10.0")),
        },
        
        # Strategy Configuration
        "strategies": {
            "enabled": os.getenv("ENABLED_STRATEGIES", "moving_average,rsi").split(","),
            "timeframe": os.getenv("STRATEGY_TIMEFRAME", "1m"),
            "ohlcv_limit": int(os.getenv("OHLCV_LIMIT", "100")),
            "moving_average": {
                "short_period": int(os.getenv("MA_SHORT_PERIOD", "5")),
                "long_period": int(os.getenv("MA_LONG_PERIOD", "10")),
            },
            "rsi": {
                "period": int(os.getenv("RSI_PERIOD", "14")),
                "overbought": float(os.getenv("RSI_OVERBOUGHT", "70")),
                "oversold": float(os.getenv("RSI_OVERSOLD", "30")),
            },
        },
        
        # Database Configuration
        "database": {
            "url": os.getenv("DATABASE_URL", "sqlite:///trading_bot.db"),
        },
        
        # Logging Configuration
        "logging": {
            "level": os.getenv("LOG_LEVEL", "INFO"),
            "file": os.getenv("LOG_FILE", "trading_bot.log"),
        },
        
        # Backtesting Configuration
        "backtesting": {
            "enabled": os.getenv("BACKTESTING_ENABLED", "false").lower() == "true",
            "start_date": os.getenv("BACKTEST_START_DATE", ""),
            "end_date": os.getenv("BACKTEST_END_DATE", ""),
        },
    }
    
    return config


def validate_config(config: Dict[str, Any]) -> bool:
    """
    Validate the configuration settings.
    
    Args:
        config: Configuration dictionary
        
    Returns:
        True if configuration is valid, False otherwise
    """
    required_fields = [
        "api.private_key",
        "api.wallet_address",
    ]
    
    for field in required_fields:
        keys = field.split(".")
        value = config
        for key in keys:
            if key not in value:
                print(f"Missing required configuration: {field}")
                return False
            value = value[key]
        
        if not value:
            print(f"Empty required configuration: {field}")
            return False
    
    # Validate trading configuration
    trading_config = config['trading']
    if trading_config['min_open_interest'] >= trading_config['max_open_interest']:
        print("MIN_OPEN_INTEREST must be less than MAX_OPEN_INTEREST")
        return False
    
    if trading_config['max_pairs_to_trade'] <= 0:
        print("MAX_PAIRS_TO_TRADE must be greater than 0")
        return False
    
    # Validate timeframe
    valid_timeframes = ['1m', '5m', '15m', '30m', '1h', '4h', '1d']
    timeframe = config['strategies']['timeframe']
    if timeframe not in valid_timeframes:
        print(f"Invalid timeframe: {timeframe}. Valid options: {valid_timeframes}")
        return False
    
    return True 