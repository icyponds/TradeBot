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
    # Phase-1: disable the 15m stat-arb instance by default (too much churn / fees).
    # Can be re-enabled explicitly for research/backtests.
    # [MODIFIED] Changed default to "true" for comprehensive backtesting
    enable_stat_arb_15m = os.getenv("ENABLE_STAT_ARB_15M", "true").lower() == "true"

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
            "max_position_size_percentage": float(os.getenv("MAX_POSITION_SIZE_PERCENTAGE", "10.0")),  # Max % of portfolio per position
            "max_positions_percentage": float(os.getenv("MAX_POSITIONS_PERCENTAGE", "80.0")),  # Max % of portfolio in all positions
            
            # Order monitoring settings
            "order_timeout_minutes": float(os.getenv("ORDER_TIMEOUT_MINUTES", ".05")),
            "enable_stale_order_cleanup": os.getenv("ENABLE_STALE_ORDER_CLEANUP", "true").lower() == "true",
            "position_sync_interval": int(os.getenv("POSITION_SYNC_INTERVAL", "1")),  # seconds
            "enable_position_validation": os.getenv("ENABLE_POSITION_VALIDATION", "true").lower() == "true",
            
            # Position monitoring settings
            "position_monitoring_interval": int(os.getenv("POSITION_MONITORING_INTERVAL", "10")),  # seconds
            
            # Global risk limit: max % of account that can be lost on any single trade
            "max_account_loss_per_trade": float(os.getenv("MAX_ACCOUNT_LOSS_PER_TRADE", "3.0")),  # 3% of account

            # Multi-leg trade controls (used by funding-rate arbitrage: spot + perp)
            "multi_leg": {
                # If true, when we don't have enough *combined* USDC across spot + perp silos
                # to fund both legs, scale the trade down to what is available.
                "auto_scale_to_funds": os.getenv("MULTI_LEG_AUTO_SCALE_TO_FUNDS", "true").lower() == "true",
                # If we would need to scale below this factor, skip the trade entirely.
                # (Prevents dust trades and excessive slippage relative to size.)
                "min_scale_factor": float(os.getenv("MULTI_LEG_MIN_SCALE_FACTOR", "0.10")),
            },
            
            # Strategy weighting configuration
            "max_positions_per_strategy": int(os.getenv("MAX_POSITIONS_PER_STRATEGY", "5")),  # Limit positions per strategy
            "min_trades_for_ranking": int(os.getenv("MIN_TRADES_FOR_RANKING", "3")),  # Faster weight adaptation
        },
        
        # Risk Management Configuration
        "risk_management": {
            "margin_buffer_percentage": float(os.getenv("MARGIN_BUFFER_PERCENTAGE", "20")),
            "liquidation_risk_threshold": float(os.getenv("LIQUIDATION_RISK_THRESHOLD", "80")),
            # Emergency portfolio stop: close all positions if portfolio loss exceeds this percent
            # Loss is computed vs total capital_at_risk across positions.
            "emergency_portfolio_loss_pct": float(os.getenv("EMERGENCY_PORTFOLIO_LOSS_PCT", "10.0")),

            # Regime detection + allocation (HMM) and change-point gating (Page-Hinkley)
            # These features are optional and default to enabled with conservative settings.
            "regime_allocator": {
                "enabled": os.getenv("REGIME_ALLOCATOR_ENABLED", "true").lower() == "true",
                "proxy_symbol": os.getenv("REGIME_PROXY_SYMBOL", "BTC"),
                "timeframe": os.getenv("REGIME_TIMEFRAME", "15m"),
                "lookback": int(os.getenv("REGIME_LOOKBACK", "220")),
                "retrain_minutes": int(os.getenv("REGIME_RETRAIN_MINUTES", "30")),
                "hysteresis_threshold": float(os.getenv("REGIME_HYSTERESIS_THRESHOLD", "0.60")),
                "min_switch_minutes": int(os.getenv("REGIME_MIN_SWITCH_MINUTES", "15")),
            },
            "change_point": {
                "enabled": os.getenv("CHANGEPOINT_ENABLED", "true").lower() == "true",
                "proxy_symbol": os.getenv("CHANGEPOINT_PROXY_SYMBOL", "BTC"),
                "timeframe": os.getenv("CHANGEPOINT_TIMEFRAME", "15m"),
                "cooldown_minutes": int(os.getenv("CHANGEPOINT_COOLDOWN_MINUTES", "20")),
                # Default: only gate fragile mean-reversion / stat-arb entries
                "apply_to_strategies": [
                    s.strip()
                    for s in os.getenv("CHANGEPOINT_APPLY_TO", "ou_mean_reversion,stat_arb").split(",")
                    if s.strip()
                ],
                # Page-Hinkley parameters (tuned for abs returns)
                "delta": float(os.getenv("CHANGEPOINT_DELTA", "0.0")),
                "threshold": float(os.getenv("CHANGEPOINT_THRESHOLD", "0.02")),
                "alpha": float(os.getenv("CHANGEPOINT_ALPHA", "0.99")),
            },

            # Strategy exploration to ensure sufficient sample sizes.
            # When multiple strategies emit a same-direction entry signal for the same symbol,
            # we occasionally route execution to under-sampled strategies with smaller sizing.
            "strategy_exploration": {
                "enabled": os.getenv("STRATEGY_EXPLORATION_ENABLED", "true").lower() == "true",
                # Chance to explore when eligible (0.0 - 1.0)
                "epsilon": float(os.getenv("STRATEGY_EXPLORATION_EPSILON", "0.15")),
                # Treat strategies with < N trades as "under-sampled"
                "min_trades_per_strategy": int(os.getenv("STRATEGY_EXPLORATION_MIN_TRADES", "20")),
                # Scale down position size on exploration executions
                "position_size_scale": float(os.getenv("STRATEGY_EXPLORATION_SIZE_SCALE", "0.30")),
                # If winner is only marginally stronger than runner-up, prefer exploring
                "close_strength_delta": float(os.getenv("STRATEGY_EXPLORATION_CLOSE_DELTA", "0.10")),
                # Reserve some capital for exploration trades so we can always gather sample size
                # (Normal trades size/check against available_capital * (1 - reserve_capital_pct))
                "reserve_capital_pct": float(os.getenv("STRATEGY_EXPLORATION_RESERVE_PCT", "0.10")),
            },

            # Phase-1: cost-aware entry gating for mean-reversion/stat-arb strategies.
            # Enforce a minimum additional Z-score over the strategy's entry threshold so we avoid
            # churning near the boundary where fees/slippage dominate.
            "entry_hurdles": {
                "enabled": os.getenv("ENTRY_HURDLES_ENABLED", "true").lower() == "true",
                "zscore_buffer": float(os.getenv("ENTRY_HURDLE_ZSCORE_BUFFER", "0.30")),
                "apply_to": [
                    s.strip()
                    for s in os.getenv("ENTRY_HURDLE_APPLY_TO", "ou_mean_reversion,stat_arb").split(",")
                    if s.strip()
                ],
            },
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

            # Phase-1: budget-vs-ceiling sizing. Caps define the ceiling; base_risk_pct defines the average.
            "base_risk_per_trade_pct": float(os.getenv("BASE_RISK_PER_TRADE_PCT", "0.50")),
            "min_risk_multiplier": float(os.getenv("MIN_RISK_MULTIPLIER", "0.25")),
            "max_risk_multiplier": float(os.getenv("MAX_RISK_MULTIPLIER", "4.00")),
            # Apply a mild volatility scaling to margin-at-risk (in addition to leverage adjustment).
            # g(vol) = (1/(1+max(vol,0)))^power
            "vol_risk_scale_power": float(os.getenv("VOL_RISK_SCALE_POWER", "0.50")),
        },
        
        # Strategy Configuration
        "strategies": {
            "enabled": os.getenv("ENABLED_STRATEGIES", "stat_arb,funding_rate_arbitrage,ou_mean_reversion").split(","),
            # Dynamic Timeframe Architecture: Define specific strategy instances
            # If 'instances' is defined, it overrides 'enabled' list for instantiation
            "instances": [
                # Parallel Strategy Execution:
                # We run multiple instances of the same strategy on different timeframes.
                # The StrategySelector will dynamically weight these based on live performance.

                # Statistical Arbitrage (15m, 1h, 4h)
                {"type": "stat_arb", "name": "stat_arb_15m", "timeframe": "15m"},
                {"type": "stat_arb", "name": "stat_arb_1h",  "timeframe": "1h"},
                {"type": "stat_arb", "name": "stat_arb_4h",  "timeframe": "4h"},

                # OU Mean Reversion (15m, 1h, 4h)
                {"type": "ou_mean_reversion", "name": "ou_mean_reversion_15m", "timeframe": "15m"},
                {"type": "ou_mean_reversion", "name": "ou_mean_reversion_1h",  "timeframe": "1h"},
                {"type": "ou_mean_reversion", "name": "ou_mean_reversion_4h",  "timeframe": "4h"},
                
                # Volatility Breakout (15m, 1h, 4h)
                {"type": "volatility_breakout", "name": "vol_breakout_15m", "timeframe": "15m"},
                {"type": "volatility_breakout", "name": "vol_breakout_1h", "timeframe": "1h"},
                {"type": "volatility_breakout", "name": "vol_breakout_4h", "timeframe": "4h"},
                
                # Adaptive Grid (15m, 1h, 4h)
                {"type": "adaptive_grid", "name": "adaptive_grid_15m", "timeframe": "15m"},
                {"type": "adaptive_grid", "name": "adaptive_grid_1h", "timeframe": "1h"},
                {"type": "adaptive_grid", "name": "adaptive_grid_4h", "timeframe": "4h"},
                
                # Sentiment ML (15m, 1h, 4h)
                {"type": "sentiment_ml", "name": "sentiment_ml_15m", "timeframe": "15m"},
                {"type": "sentiment_ml", "name": "sentiment_ml_1h", "timeframe": "1h"},
                {"type": "sentiment_ml", "name": "sentiment_ml_4h", "timeframe": "4h"},
                
                # Liquidation Hunter (5m, 15m, 1h)
                {"type": "liquidation_hunter", "name": "liquidation_hunter_5m", "timeframe": "5m"},
                {"type": "liquidation_hunter", "name": "liquidation_hunter_15m", "timeframe": "15m"},
                {"type": "liquidation_hunter", "name": "liquidation_hunter_1h", "timeframe": "1h"},

                # Cross-Sectional Momentum (1h, 4h)
                {"type": "cross_sectional_momentum", "name": "csm_1h", "timeframe": "1h"},
                {"type": "cross_sectional_momentum", "name": "csm_4h", "timeframe": "4h"},
            ],
            # Note: timeframe is now auto-selected per strategy instance
            "ohlcv_limit": int(os.getenv("OHLCV_LIMIT", "300")), # Increased for EMA200
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
                "entry_threshold": float(os.getenv("FR_ARB_ENTRY_THRESHOLD", "0.00002")),  # Lowered to ~17.5% APR
                "exit_threshold": float(os.getenv("FR_ARB_EXIT_THRESHOLD", "0.00001")),    # Lowered slightly
                "max_position_pct": float(os.getenv("FR_ARB_MAX_POSITION_PCT", "20")),
                "min_holding_periods": int(os.getenv("FR_ARB_MIN_HOLDING_PERIODS", "1")),
                "rebalance_threshold": float(os.getenv("FR_ARB_REBALANCE_THRESHOLD", "0.02")),
                "funding_history_periods": int(os.getenv("FR_ARB_HISTORY_PERIODS", "24")),
                "min_consistent_periods": int(os.getenv("FR_ARB_MIN_CONSISTENT_PERIODS", "1")), # Reduced to capture spikes
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
            

            
            # Volatility Breakout Strategy
            "volatility_breakout": {
                "bb_length": int(os.getenv("VB_BB_LENGTH", "20")),
                "bb_std": float(os.getenv("VB_BB_STD", "2.0")),
                "squeeze_threshold": float(os.getenv("VB_SQUEEZE_THRESHOLD", "0.10")),
                "atr_length": int(os.getenv("VB_ATR_LENGTH", "14")),
                "atr_multiplier_sl": float(os.getenv("VB_ATR_MULT_SL", "2.0")),
                "atr_multiplier_tp": float(os.getenv("VB_ATR_MULT_TP", "4.0")),
            },
            
            # Adaptive Grid Strategy
            "adaptive_grid": {
                "ema_period": int(os.getenv("GRID_EMA_PERIOD", "50")),
                "atr_period": int(os.getenv("GRID_ATR_PERIOD", "14")),
                "grid_spacing_atr": float(os.getenv("GRID_SPACING_ATR", "2.5")), # Increased from 1.5 to reduce churn
            },
            
            # Sentiment ML Strategy
            "sentiment_ml": {
                "sentiment_threshold": float(os.getenv("SENTIMENT_THRESHOLD", "2.0")),
                "normalization_lookback": int(os.getenv("SENTIMENT_LOOKBACK", "168")),
            },
            
            # Liquidation Hunter Strategy
            "liquidation_hunter": {
                "window": int(os.getenv("LH_WINDOW", "20")),
                "std_dev_threshold": float(os.getenv("LH_STD_DEV_THRESHOLD", "3.0")),
            },
            
            # Cross-Sectional Momentum Strategy
            "cross_sectional_momentum": {
                "lookback_period": 12,     # 12 * 4h = 48 hours (2 days)
                "top_n_percent": 0.15,
                "bottom_n_percent": 0.15,
                "rebalance_interval": 4,   # Rebalance every 4 hours
            },
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

        # Persistence Configuration (optional)
        # NOTE: Backtests override this to write results to `data/backtest_results.db`.
        "persistence": {
            "db_path": (os.getenv("PERSISTENCE_DB_PATH", "").strip() or None),
        },
        
        # Backtesting Configuration
        "backtesting": {
            "enabled": os.getenv("BACKTESTING_ENABLED", "false").lower() == "true",
            "start_date": os.getenv("BACKTEST_START_DATE", "2024-01-01"),
            "end_date": os.getenv("BACKTEST_END_DATE", "2024-12-31"),
            "results_db_path": os.getenv("BACKTEST_RESULTS_DB_PATH", "data/backtest_results.db"),
            "reset_results_db": os.getenv("BACKTEST_RESET_RESULTS_DB", "true").lower() == "true",
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
            
            # Clustering for advanced diversification (Phase 10)
            "cluster_selection": {
                "enabled": os.getenv("CLUSTER_SELECTION_ENABLED", "true").lower() == "true",
                "k_clusters": int(os.getenv("CLUSTER_K", "5")),
                "lookback_days": int(os.getenv("CLUSTER_LOOKBACK", "30")),
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
        # USD-denominated hard caps are intentionally not used; sizing is percentage-based.
        
        if config['trading']['max_position_size_percentage'] <= 0 or config['trading']['max_position_size_percentage'] > 100:
            print("ERROR: Max position size percentage must be between 0 and 100")
            return False
        
        # Note: Strategy timeframes are auto-selected per strategy
        
        return True
        
    except Exception as e:
        print(f"ERROR: Configuration validation failed: {e}")
        return False 