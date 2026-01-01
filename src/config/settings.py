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
            "private_key": os.getenv("HYPERLIQUID_PRIVATE_KEY", ""),
            "wallet_address": os.getenv("HYPERLIQUID_WALLET_ADDRESS", ""),
            "public_account_address": os.getenv("HYPERLIQUID_PUBLIC_ACCOUNT_ADDRESS", ""),
            "timeout": int(os.getenv("API_TIMEOUT", "30")),
            
            # Rate limiting configuration
            "rate_limit": {
                "calls_per_second": float(os.getenv("API_RATE_LIMIT_CPS", "10")),
                "burst_size": int(os.getenv("API_RATE_LIMIT_BURST", "20")),
            },
            
            # Circuit breaker configuration
            "circuit_breaker": {
                "failure_threshold": int(os.getenv("API_CB_FAILURE_THRESHOLD", "5")),
                "recovery_timeout": float(os.getenv("API_CB_RECOVERY_TIMEOUT", "30.0")),
            },
            
            # Cache configuration (TTL in seconds)
            "cache": {
                "default_ttl": float(os.getenv("API_CACHE_DEFAULT_TTL", "1.0")),
                "market_data_ttl": float(os.getenv("API_CACHE_MARKET_DATA_TTL", "0.5")),
                "asset_info_ttl": float(os.getenv("API_CACHE_ASSET_INFO_TTL", "5.0")),
                "positions_ttl": float(os.getenv("API_CACHE_POSITIONS_TTL", "1.0")),
            },
            
            # Health monitoring configuration
            "health_monitor": {
                "check_interval": float(os.getenv("API_HEALTH_CHECK_INTERVAL", "30.0")),
                "unhealthy_threshold": int(os.getenv("API_HEALTH_UNHEALTHY_THRESHOLD", "3")),
                "ws_stale_threshold": float(os.getenv("API_HEALTH_WS_STALE_THRESHOLD", "60.0")),
                "latency_warning_ms": float(os.getenv("API_HEALTH_LATENCY_WARNING_MS", "1000.0")),
            },
        },
        
        # HIP-3 Perps Configuration (Builder-deployed perpetual markets)
        "hip3": {
            "enabled": os.getenv("HIP3_ENABLED", "true").lower() == "true",
            # List of HIP-3 dex names to enable, comma-separated
            # Empty/None = auto-discover all available dexes when enabled
            # Examples: "HyperEVM,SomeOtherDex"
            "perp_dexs": [
                dex.strip() for dex in os.getenv("HIP3_PERP_DEXS", "").split(",")
                if dex.strip()
            ] or None,
            # Whether to include HIP-3 assets in pair selection
            "include_in_pair_selection": os.getenv("HIP3_INCLUDE_IN_PAIR_SELECTION", "true").lower() == "true",
            # Minimum volume for HIP-3 assets (often lower liquidity)
            "min_volume": float(os.getenv("HIP3_MIN_VOLUME", "10000")),
            # Maximum leverage for HIP-3 (often more volatile)
            "max_leverage": float(os.getenv("HIP3_MAX_LEVERAGE", "5")),
            # Minimum open interest for HIP-3 perps (usually lower than native)
            "min_open_interest": float(os.getenv("HIP3_MIN_OPEN_INTEREST", "100000")),
        },
        
        # Spot Trading Configuration
        "spot": {
            "enabled": os.getenv("SPOT_ENABLED", "true").lower() == "true",
            # Whether to include spot pairs in dynamic pair selection
            "include_in_pair_selection": os.getenv("SPOT_INCLUDE_IN_PAIR_SELECTION", "true").lower() == "true",
            # Minimum 24h volume for spot pairs
            "min_volume": float(os.getenv("SPOT_MIN_VOLUME", "50000")),
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
            "max_position_size_percentage": float(os.getenv("MAX_POSITION_SIZE_PERCENTAGE", "20.0")),  # Max % of portfolio per position
            "max_positions_percentage": float(os.getenv("MAX_POSITIONS_PERCENTAGE", "80.0")),  # Max % of portfolio in all positions
            "risk_percentage": float(os.getenv("RISK_PERCENTAGE", "10.0")),
            "stop_loss_percentage": float(os.getenv("STOP_LOSS_PERCENTAGE", "5.0")),
            
            # Order monitoring settings
            "order_timeout_minutes": float(os.getenv("ORDER_TIMEOUT_MINUTES", ".05")),
            "enable_stale_order_cleanup": os.getenv("ENABLE_STALE_ORDER_CLEANUP", "true").lower() == "true",
            "position_sync_interval": int(os.getenv("POSITION_SYNC_INTERVAL", "1")),  # seconds
            "enable_position_validation": os.getenv("ENABLE_POSITION_VALIDATION", "true").lower() == "true",
            
            # Position monitoring settings
            "position_monitoring_interval": int(os.getenv("POSITION_MONITORING_INTERVAL", "10")),  # seconds
            "position_timeout_hours": float(os.getenv("POSITION_TIMEOUT_HOURS", "24")),  # (Deprecated) hours - not used
            "max_loss_percentage": float(os.getenv("MAX_LOSS_PERCENTAGE", "5.0")),  # percentage per position
            "max_profit_percentage": float(os.getenv("MAX_PROFIT_PERCENTAGE", "20.0")),  # percentage
            "emergency_loss_threshold": float(os.getenv("EMERGENCY_LOSS_THRESHOLD", "10.0")),  # percentage
            
            # Global risk limit: max % of account that can be lost on any single trade
            "max_account_loss_per_trade": float(os.getenv("MAX_ACCOUNT_LOSS_PER_TRADE", "3.0")),  # 3% of account
        },
        
        # Risk Management Configuration
        "risk_management": {
            "margin_buffer_percentage": float(os.getenv("MARGIN_BUFFER_PERCENTAGE", "20")),
            "liquidation_risk_threshold": float(os.getenv("LIQUIDATION_RISK_THRESHOLD", "80")),
        },
        
        # Leverage Management Configuration
        "leverage_management": {
            "base_leverage": float(os.getenv("LEVERAGE_BASE", "3.0")),  # Increased from 2.0
            "signal_adjustment_max": float(os.getenv("LEVERAGE_SIGNAL_ADJUSTMENT_MAX", "0.5")),
            "volatility_min": float(os.getenv("LEVERAGE_VOLATILITY_MIN", "0.1")),
            "min_leverage": float(os.getenv("LEVERAGE_MIN", "1.5")),  # Increased from 1.0
            "max_leverage": float(os.getenv("LEVERAGE_MAX", "5.0")),  # Reduced from 10.0 for safety
            "ma_strategy_adjustment": float(os.getenv("LEVERAGE_MA_STRATEGY_ADJUSTMENT", "1.1")),
            "rsi_strategy_adjustment": float(os.getenv("LEVERAGE_RSI_STRATEGY_ADJUSTMENT", "0.9")),
        },
        
        # Strategy Configuration
        "strategies": {
            "enabled": os.getenv("ENABLED_STRATEGIES", "stat_arb,funding_rate_arbitrage,ou_mean_reversion,momentum_factor").split(","),
            # Note: timeframe is now auto-selected per strategy (see each strategy class)
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
                "min_correlation": float(os.getenv("STAT_ARB_MIN_CORRELATION", "0.8")),
                "correlation_lookback": int(os.getenv("STAT_ARB_CORRELATION_LOOKBACK", "100")),
                "update_interval_hours": int(os.getenv("STAT_ARB_UPDATE_INTERVAL_HOURS", "24")),
            },
            
            # Cointegration-enhanced Statistical Arbitrage
            "cointegration": {
                "adf_pvalue_threshold": float(os.getenv("COINT_ADF_PVALUE", "0.05")),
                "half_life_max_hours": float(os.getenv("COINT_HALF_LIFE_MAX", "48")),
                "zscore_entry": float(os.getenv("COINT_ZSCORE_ENTRY", "1.5")),  # Lowered from 2.0
                "zscore_exit": float(os.getenv("COINT_ZSCORE_EXIT", "0.3")),    # Lowered from 0.5
                "lookback_period": int(os.getenv("COINT_LOOKBACK_PERIOD", "20")),
                "kalman_filter_enabled": os.getenv("COINT_KALMAN_ENABLED", "true").lower() == "true",
                "kalman_Q": float(os.getenv("COINT_KALMAN_Q", "0.001")),
                "kalman_R": float(os.getenv("COINT_KALMAN_R", "1.0")),
            },
            
            # Funding Rate Arbitrage Strategy
            "funding_rate_arbitrage": {
                "entry_threshold": float(os.getenv("FR_ARB_ENTRY_THRESHOLD", "0.00005")),  # 0.005% per 8h = 5.5% APR
                "exit_threshold": float(os.getenv("FR_ARB_EXIT_THRESHOLD", "0.00002")),    # 0.002% per 8h
                "max_position_pct": float(os.getenv("FR_ARB_MAX_POSITION_PCT", "20")),
                "min_holding_periods": int(os.getenv("FR_ARB_MIN_HOLDING_PERIODS", "1")),
                "rebalance_threshold": float(os.getenv("FR_ARB_REBALANCE_THRESHOLD", "0.02")),
                "funding_history_periods": int(os.getenv("FR_ARB_HISTORY_PERIODS", "24")),
                "min_consistent_periods": int(os.getenv("FR_ARB_MIN_CONSISTENT_PERIODS", "3")),
            },
            
            # Ornstein-Uhlenbeck Mean Reversion Strategy
            "ou_mean_reversion": {
                "zscore_entry": float(os.getenv("OU_ZSCORE_ENTRY", "1.5")),  # Lowered from 2.0
                "zscore_exit": float(os.getenv("OU_ZSCORE_EXIT", "0.3")),    # Lowered from 0.5
                "half_life_max_hours": float(os.getenv("OU_HALF_LIFE_MAX", "48")),
                "half_life_min_hours": float(os.getenv("OU_HALF_LIFE_MIN", "1")),
                "min_mean_reversion_speed": float(os.getenv("OU_MIN_THETA", "0.1")),
                "estimation_lookback": int(os.getenv("OU_ESTIMATION_LOOKBACK", "100")),
                "min_data_points": int(os.getenv("OU_MIN_DATA_POINTS", "50")),
                "cache_ttl_hours": int(os.getenv("OU_CACHE_TTL_HOURS", "4")),
            },
            
            # Cross-Sectional Momentum Factor Strategy
            "momentum_factor": {
                "lookback_days": int(os.getenv("MOMENTUM_LOOKBACK_DAYS", "7")),
                "top_n": int(os.getenv("MOMENTUM_TOP_N", "3")),
                "bottom_n": int(os.getenv("MOMENTUM_BOTTOM_N", "3")),
                "rebalance_hours": int(os.getenv("MOMENTUM_REBALANCE_HOURS", "168")),  # Weekly
                "min_assets": int(os.getenv("MOMENTUM_MIN_ASSETS", "10")),
                "min_data_points": int(os.getenv("MOMENTUM_MIN_DATA_POINTS", "24")),
                "min_volume_filter": float(os.getenv("MOMENTUM_MIN_VOLUME", "100000")),
                "exclude_extreme_returns": os.getenv("MOMENTUM_EXCLUDE_EXTREME", "true").lower() == "true",
                "extreme_return_threshold": float(os.getenv("MOMENTUM_EXTREME_THRESHOLD", "0.5")),
                "cache_ttl_hours": int(os.getenv("MOMENTUM_CACHE_TTL_HOURS", "1")),
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
            "status_timeout": int(os.getenv("ORDER_STATUS_TIMEOUT", "30")),
        },
        
        
        # Data Collection Configuration
        "data_collection": {
            "price_history_max_length": int(os.getenv("PRICE_HISTORY_MAX_LENGTH", "1000")),
            "ohlcv_history_max_length": int(os.getenv("OHLCV_HISTORY_MAX_LENGTH", "1000")),
        },
        
        # Pair Selection Configuration
        "pair_selection": {
            # Basic thresholds
            "min_volume_threshold": float(os.getenv("MIN_VOLUME_THRESHOLD", "10000")),
            "min_volume_threshold_strict": float(os.getenv("MIN_VOLUME_THRESHOLD_STRICT", "100000")),
            "volume_normalization_factor": float(os.getenv("VOLUME_NORMALIZATION_FACTOR", "1000000")),
            
            # Selection mode: simple, sophisticated, momentum, mean_reversion, balanced
            "mode": os.getenv("PAIR_SELECTION_MODE", "sophisticated"),
            
            # Score weights (must sum to 1.0) - only used in "sophisticated" mode
            "weights": {
                "liquidity": float(os.getenv("PAIR_WEIGHT_LIQUIDITY", "0.25")),
                "volatility": float(os.getenv("PAIR_WEIGHT_VOLATILITY", "0.20")),
                "strategy_fit": float(os.getenv("PAIR_WEIGHT_STRATEGY_FIT", "0.25")),
                "diversification": float(os.getenv("PAIR_WEIGHT_DIVERSIFICATION", "0.15")),
                "historical_performance": float(os.getenv("PAIR_WEIGHT_HISTORICAL", "0.15")),
            },
            
            # Volatility parameters (optimal daily volatility range)
            "volatility": {
                "optimal_min": float(os.getenv("PAIR_VOL_OPTIMAL_MIN", "0.02")),  # 2%
                "optimal_max": float(os.getenv("PAIR_VOL_OPTIMAL_MAX", "0.08")),  # 8%
                "lookback_days": int(os.getenv("PAIR_VOL_LOOKBACK_DAYS", "14")),
            },
            
            # Diversification parameters
            "diversification": {
                "max_correlation": float(os.getenv("PAIR_MAX_CORRELATION", "0.7")),
                "penalty_factor": float(os.getenv("PAIR_CORRELATION_PENALTY", "0.5")),
            },
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
        
        # Note: ws_url is no longer required - SDK handles WebSocket automatically
        
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
        
        # Note: Strategy timeframes are auto-selected per strategy
        
        return True
        
    except Exception as e:
        print(f"ERROR: Configuration validation failed: {e}")
        return False 