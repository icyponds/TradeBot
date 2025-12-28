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
            "base_url": os.getenv("HYPERLIQUID_API_URL"),
            "ws_url": os.getenv("HYPERLIQUID_WS_URL"),
            "ws_alternative_urls": [
                url.strip() for url in os.getenv("HYPERLIQUID_WS_ALTERNATIVE_URLS", "").split(",")
                if url.strip()
            ],
            "private_key": os.getenv("HYPERLIQUID_PRIVATE_KEY", ""),
            "wallet_address": os.getenv("HYPERLIQUID_WALLET_ADDRESS", ""),
            "public_account_address": os.getenv("HYPERLIQUID_PUBLIC_ACCOUNT_ADDRESS", ""),
            "timeout": int(os.getenv("API_TIMEOUT", "30")),
        },
        
        # Trading Configuration
        "trading": {
            "symbols": [],  # Empty list for dynamic pair selection
            "dynamic_pair_selection": os.getenv("DYNAMIC_PAIR_SELECTION", "true").lower() == "true",
            "min_open_interest": float(os.getenv("MIN_OPEN_INTEREST", "1000000")),
            "scan_interval_minutes": int(os.getenv("SCAN_INTERVAL_MINUTES", "60")),
            "excluded_assets": [asset.strip() for asset in os.getenv("EXCLUDED_ASSETS", "").split(",") if asset.strip()],
            "included_assets": [asset.strip() for asset in os.getenv("INCLUDED_ASSETS", "").split(",") if asset.strip()],
            "base_currency": os.getenv("BASE_CURRENCY", "USDC"),
            
            # Portfolio-based position sizing
            "use_portfolio_based_sizing": os.getenv("USE_PORTFOLIO_BASED_SIZING", "true").lower() == "true",
            "max_position_size_usd": float(os.getenv("MAX_POSITION_SIZE_USD", "100")),  # Fallback max USD per position
            "max_position_size_percentage": float(os.getenv("MAX_POSITION_SIZE_PERCENTAGE", "10.0")),  # Max % of portfolio per position
            "max_positions_percentage": float(os.getenv("MAX_POSITIONS_PERCENTAGE", "33.33")),  # Max % of portfolio in all positions
            "risk_percentage": float(os.getenv("RISK_PERCENTAGE", "10.0")),
            "stop_loss_percentage": float(os.getenv("STOP_LOSS_PERCENTAGE", "5.0")),
            
            # Order monitoring settings
            "order_timeout_minutes": float(os.getenv("ORDER_TIMEOUT_MINUTES", ".05")),
            "enable_stale_order_cleanup": os.getenv("ENABLE_STALE_ORDER_CLEANUP", "true").lower() == "true",
            "position_sync_interval": int(os.getenv("POSITION_SYNC_INTERVAL", "1")),  # seconds
            "enable_position_validation": os.getenv("ENABLE_POSITION_VALIDATION", "true").lower() == "true",
            
            # Position monitoring settings
            "position_monitoring_interval": int(os.getenv("POSITION_MONITORING_INTERVAL", "10")),  # seconds
            "position_timeout_hours": float(os.getenv("POSITION_TIMEOUT_HOURS", "24")),  # hours
            "max_loss_percentage": float(os.getenv("MAX_LOSS_PERCENTAGE", "5.0")),  # percentage
            "max_profit_percentage": float(os.getenv("MAX_PROFIT_PERCENTAGE", "20.0")),  # percentage
            "emergency_loss_threshold": float(os.getenv("EMERGENCY_LOSS_THRESHOLD", "10.0")),  # percentage
        },
        
        # Risk Management Configuration
        "risk_management": {
            "margin_buffer_percentage": float(os.getenv("MARGIN_BUFFER_PERCENTAGE", "20")),
            "liquidation_risk_threshold": float(os.getenv("LIQUIDATION_RISK_THRESHOLD", "80")),
        },
        
        # Leverage Management Configuration
        "leverage_management": {
            "base_leverage": float(os.getenv("LEVERAGE_BASE", "2.0")),
            "signal_adjustment_max": float(os.getenv("LEVERAGE_SIGNAL_ADJUSTMENT_MAX", "0.5")),
            "volatility_min": float(os.getenv("LEVERAGE_VOLATILITY_MIN", "0.1")),
            "min_leverage": float(os.getenv("LEVERAGE_MIN", "1.0")),
            "max_leverage": float(os.getenv("LEVERAGE_MAX", "10.0")),
            "ma_strategy_adjustment": float(os.getenv("LEVERAGE_MA_STRATEGY_ADJUSTMENT", "1.1")),
            "rsi_strategy_adjustment": float(os.getenv("LEVERAGE_RSI_STRATEGY_ADJUSTMENT", "0.9")),
        },
        
        # Strategy Configuration
        "strategies": {
            "enabled": os.getenv("ENABLED_STRATEGIES", "moving_average,rsi,bollinger_band,supertrend,vwap,stat_arb").split(","),
            "timeframe": os.getenv("STRATEGY_TIMEFRAME", "1m"),
            "ohlcv_limit": int(os.getenv("OHLCV_LIMIT", "100")),
            "moving_average": {
                "short_period": int(os.getenv("MA_SHORT_PERIOD", "5")),
                "long_period": int(os.getenv("MA_LONG_PERIOD", "10")),
                "trend_strength_multiplier": float(os.getenv("MA_TREND_STRENGTH_MULTIPLIER", "10")),
                "volatility_cap": float(os.getenv("MA_VOLATILITY_CAP", "2.0")),
                "base_take_profit_percentage": float(os.getenv("MA_BASE_TAKE_PROFIT_PERCENTAGE", "0.06")),
                "trend_adjustment_max": float(os.getenv("MA_TREND_ADJUSTMENT_MAX", "0.5")),
                "volatility_adjustment_max": float(os.getenv("MA_VOLATILITY_ADJUSTMENT_MAX", "0.3")),
                "take_profit_min": float(os.getenv("MA_TAKE_PROFIT_MIN", "0.02")),
                "take_profit_max": float(os.getenv("MA_TAKE_PROFIT_MAX", "0.15")),
            },
            "rsi": {
                "period": int(os.getenv("RSI_PERIOD", "14")),
                "overbought": float(os.getenv("RSI_OVERBOUGHT", "70")),
                "oversold": float(os.getenv("RSI_OVERSOLD", "30")),
            },
            "bollinger_band": {
                "period": int(os.getenv("BB_PERIOD", "20")),
                "std_dev": float(os.getenv("BB_STD_DEV", "2.0")),
                "kc_mult": float(os.getenv("BB_KC_MULT", "1.5")),
            },
            "supertrend": {
                "atr_period": int(os.getenv("SUPERTREND_ATR_PERIOD", "10")),
                "multiplier": float(os.getenv("SUPERTREND_MULTIPLIER", "3.0")),
            },
            "vwap": {
                "std_dev_mult": float(os.getenv("VWAP_STD_DEV_MULT", "2.0")),
                "rsi_period": int(os.getenv("VWAP_RSI_PERIOD", "14")),
                "rsi_overbought": float(os.getenv("VWAP_RSI_OVERBOUGHT", "70")),
                "rsi_oversold": float(os.getenv("VWAP_RSI_OVERSOLD", "30")),
            },
            "stat_arb": {
                "z_score_threshold": float(os.getenv("STAT_ARB_Z_SCORE_THRESHOLD", "2.0")),
                "window_size": int(os.getenv("STAT_ARB_WINDOW_SIZE", "100")),
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
            "purge_logs": os.getenv("PURGE_LOGS_ON_STARTUP", "true").lower() == "true",
            "clear_current_log": os.getenv("CLEAR_CURRENT_LOG_ON_STARTUP", "true").lower() == "true",
            "max_log_files": int(os.getenv("MAX_LOG_FILES", "10")),
            "max_log_age_days": int(os.getenv("MAX_LOG_AGE_DAYS", "7")),
            "max_file_size_mb": int(os.getenv("MAX_LOG_FILE_SIZE_MB", "50")),
        },
        
        # Backtesting Configuration
        "backtesting": {
            "enabled": os.getenv("BACKTESTING_ENABLED", "false").lower() == "true",
            "start_date": os.getenv("BACKTEST_START_DATE", "2024-01-01"),
            "end_date": os.getenv("BACKTEST_END_DATE", "2024-12-31"),
        },
        
        # Order Management Configuration
        "order_management": {
            "walk_max_attempts": int(os.getenv("ORDER_WALK_MAX_ATTEMPTS", "5")),
            "walk_price_step": float(os.getenv("ORDER_WALK_PRICE_STEP", "0.02")),
            "walk_delay": float(os.getenv("ORDER_WALK_DELAY", "0.5")),
            "status_timeout": int(os.getenv("ORDER_STATUS_TIMEOUT", "30")),
        },
        
        # WebSocket Configuration
        "websocket": {
            "reconnect_max_attempts": int(os.getenv("WS_RECONNECT_MAX_ATTEMPTS", "5")),
            "reconnect_backoff_base": int(os.getenv("WS_RECONNECT_BACKOFF_BASE", "2")),
            "connection_timeout": int(os.getenv("WS_CONNECTION_TIMEOUT", "10")),
        },
        
        # Data Collection Configuration
        "data_collection": {
            "price_history_max_length": int(os.getenv("PRICE_HISTORY_MAX_LENGTH", "1000")),
            "ohlcv_history_max_length": int(os.getenv("OHLCV_HISTORY_MAX_LENGTH", "1000")),
        },
        
        # Pair Selection Configuration
        "pair_selection": {
            "min_volume_threshold": float(os.getenv("MIN_VOLUME_THRESHOLD", "10000")),
            "min_volume_threshold_strict": float(os.getenv("MIN_VOLUME_THRESHOLD_STRICT", "100000")),
            "volume_normalization_factor": float(os.getenv("VOLUME_NORMALIZATION_FACTOR", "1000000")),
        },
        
        # RSI Strategy Configuration
        "rsi_strategy": {
            "oversold_threshold": float(os.getenv("RSI_OVERSOLD_THRESHOLD", "30")),
            "overbought_threshold": float(os.getenv("RSI_OVERBOUGHT_THRESHOLD", "70")),
            "neutral_threshold": float(os.getenv("RSI_NEUTRAL_THRESHOLD", "60")),
            "factor_oversold": float(os.getenv("RSI_FACTOR_OVERSOLD", "1.3")),
            "factor_overbought": float(os.getenv("RSI_FACTOR_OVERBOUGHT", "0.7")),
            "volatility_adjustment_max": float(os.getenv("RSI_VOLATILITY_ADJUSTMENT_MAX", "0.3")),
        },
        
    }
    
    return config


def validate_config(config: Dict[str, Any]) -> bool:
    """
    Validate configuration settings.
    
    Args:
        config: Configuration dictionary
        
    Returns:
        True if valid, False otherwise
    """
    try:
        # Validate API configuration
        if not config['api']['base_url']:
            print("ERROR: HYPERLIQUID_API_URL environment variable is required")
            return False
        
        if not config['api']['ws_url']:
            print("ERROR: HYPERLIQUID_WS_URL environment variable is required")
            return False
        
        if not config['api']['private_key'] or not config['api']['wallet_address']:
            print("ERROR: Hyperliquid private key and wallet address are required")
            return False
        
        # Validate trading configuration
        if config['trading']['max_position_size_usd'] <= 0:
            print("ERROR: Max position size USD must be positive")
            return False
        
        if config['trading']['max_position_size_percentage'] <= 0 or config['trading']['max_position_size_percentage'] > 100:
            print("ERROR: Max position size percentage must be between 0 and 100")
            return False
        
        if config['trading']['risk_percentage'] <= 0 or config['trading']['risk_percentage'] > 100:
            print("ERROR: Risk percentage must be between 0 and 100")
            return False
        
        # Validate strategy configuration
        valid_timeframes = ['1m', '5m', '15m', '30m', '1h', '4h', '1d']
        if config['strategies']['timeframe'] not in valid_timeframes:
            print(f"ERROR: Invalid timeframe. Must be one of: {valid_timeframes}")
            return False
        
        return True
        
    except Exception as e:
        print(f"ERROR: Configuration validation failed: {e}")
        return False 