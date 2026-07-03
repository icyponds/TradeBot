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
            
            # Rate limiting configuration (Hyperliquid request-weight units:
            # the API allows 1200 weight/min per IP; cheap info requests cost 2,
            # most info requests cost 20). 15/s = 900/min leaves headroom.
            "rate_limit": {
                "calls_per_second": float(os.getenv("API_RATE_LIMIT_CPS", "15")),
                "burst_size": int(os.getenv("API_RATE_LIMIT_BURST", "60")),
            },
            
            # Circuit breaker configuration
            "circuit_breaker": {
                "failure_threshold": int(os.getenv("API_CB_FAILURE_THRESHOLD", "5")),
                "recovery_timeout": float(os.getenv("API_CB_RECOVERY_TIMEOUT", "30.0")),
            },
            
            # Cache configuration (TTL in seconds)
            # market_data_ttl: the analysis cadence is 15s, so sub-second TTLs
            # just multiply full-universe metaAndAssetCtxs fetches for nothing.
            "cache": {
                "default_ttl": float(os.getenv("API_CACHE_DEFAULT_TTL", "1.0")),
                "market_data_ttl": float(os.getenv("API_CACHE_MARKET_DATA_TTL", "5.0")),
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
            
            # API Key Expiration Monitoring
            # Set explicit expiration date (YYYY-MM-DD) OR creation date (YYYY-MM-DD)
            # Default to the specific date requested by user if env var not set
            "key_created_at": os.getenv("TRADING_API_KEY_CREATED_AT", ""),
            "key_expiration_date": os.getenv("TRADING_API_KEY_EXPIRATION_DATE", "2026-07-30"),
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
            "max_pairs_to_trade": int(os.getenv("MAX_PAIRS_TO_TRADE", "50")),
            "excluded_assets": [asset.strip() for asset in os.getenv("EXCLUDED_ASSETS", "").split(",") if asset.strip()],
            "included_assets": [asset.strip() for asset in os.getenv("INCLUDED_ASSETS", "").split(",") if asset.strip()],
            "base_currency": os.getenv("BASE_CURRENCY", "USDC"),
            
            # Portfolio-based position sizing
            "use_portfolio_based_sizing": os.getenv("USE_PORTFOLIO_BASED_SIZING", "true").lower() == "true",
            "max_position_size_percentage": float(os.getenv("MAX_POSITION_SIZE_PERCENTAGE", "20.0")),  # Max % of portfolio per position (Reduced by 50% per trade analysis)
            "max_positions_percentage": float(os.getenv("MAX_POSITIONS_PERCENTAGE", "90.0")),  # Max % of portfolio in all positions
            
            # Order monitoring settings
            "order_timeout_minutes": float(os.getenv("ORDER_TIMEOUT_MINUTES", ".05")),
            "enable_stale_order_cleanup": os.getenv("ENABLE_STALE_ORDER_CLEANUP", "true").lower() == "true",
            # Full exchange sync (user_state per DEX) cadence. Real-time position
            # changes arrive via WebSocket; this is only a ghost-position safety
            # net, so 60s is plenty (1s effectively meant "every single cycle").
            "position_sync_interval": int(os.getenv("POSITION_SYNC_INTERVAL", "60")),  # seconds
            "enable_position_validation": os.getenv("ENABLE_POSITION_VALIDATION", "true").lower() == "true",
            
            # Position monitoring settings
            "position_monitoring_interval": int(os.getenv("POSITION_MONITORING_INTERVAL", "10")),  # seconds
            
            # Global risk limit: max % of account that can be lost on any single trade
            "max_account_loss_per_trade": float(os.getenv("MAX_ACCOUNT_LOSS_PER_TRADE", "3.0")),  # 3% of account

            # Post-only (ALO) maker ENTRIES — live analogue of the round-4
            # backtest model (backtesting.maker_execution): rest at the touch,
            # unfilled after timeout = missed entry, never chased with taker.
            # Exits and reduce-only orders are unaffected (always taker).
            # Maker fills cut ~10bps/leg to ~1.5bps; at csm's trade rate that
            # roughly doubles the validated 7-window total (round 4: 5-seed
            # ensemble all positive, mean +$17k..+$22k). Default OFF pending
            # live validation at small size.
            "maker_entries": {
                "enabled": False,
                # Instance names to route through maker entries; empty = all
                # single-leg perp/HIP-3 entries.
                "strategies": [],
                "timeout_seconds": 300.0,
                "poll_interval_seconds": 5.0,
            },

            # Multi-leg trade controls (used by funding-rate arbitrage: spot + perp)
            "multi_leg": {
                # If true, when we don't have enough *combined* USDC across spot + perp silos
                # to fund both legs, scale the trade down to what is available.
                "auto_scale_to_funds": os.getenv("MULTI_LEG_AUTO_SCALE_TO_FUNDS", "true").lower() == "true",
                # If we would need to scale below this factor, skip the trade entirely.
                # (Prevents dust trades and excessive slippage relative to size.)
                "min_scale_factor": float(os.getenv("MULTI_LEG_MIN_SCALE_FACTOR", "0.10")),
            },

            # Per-strategy cooldowns (seconds) to reduce churn on high-turnover strategies
            "strategy_cooldowns": {
                "adaptive_grid_5m": int(os.getenv("COOLDOWN_ADAPTIVE_GRID_5M", "120")),
                "adaptive_grid_15m": int(os.getenv("COOLDOWN_ADAPTIVE_GRID_15M", "180")),
                "vol_breakout_15m": int(os.getenv("COOLDOWN_VOL_BREAKOUT_15M", "180")),
                "vol_breakout_1h": int(os.getenv("COOLDOWN_VOL_BREAKOUT_1H", "300")),
                "vol_breakout_4h": int(os.getenv("COOLDOWN_VOL_BREAKOUT_4H", "600")),
                "liquidation_hunter_5m": int(os.getenv("COOLDOWN_LH_5M", "120")),
                "liquidation_hunter_15m": int(os.getenv("COOLDOWN_LH_15M", "180")),
                "sentiment_ml_15m": int(os.getenv("COOLDOWN_SENTIMENT_15M", "180")),
            },

            # Pair blacklist/penalties to avoid or downweight problematic symbols
            "pair_blacklist": [
                asset.strip()
                for asset in os.getenv("PAIR_BLACKLIST", "kBONK").split(",")
                if asset.strip()
            ],
            # Symbol blacklist per trade performance analysis
            "symbol_blacklist": {
                "global": ["kBONK", "DASH", "UNI"],  # Worst performers
                "stat_arb": ["BTC", "XRP", "ZEC", "AVAX", "ASTER"],  # Poor R/R ratio despite wins
            },
            "pair_penalties": {
                # symbol: scale (0-1). Example env: PAIR_PENALTY_STABLE=0.2
                "STABLE": float(os.getenv("PAIR_PENALTY_STABLE", "1.0")),
                "STABLE_SPOT": float(os.getenv("PAIR_PENALTY_STABLE_SPOT", "1.0")),
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
            # NOTE: defaults to disabled - with a single enabled strategy there is
            # nothing to explore and the reserve just idles capital. Re-enable when
            # multiple strategy instances are active.
            "strategy_exploration": {
                "enabled": os.getenv("STRATEGY_EXPLORATION_ENABLED", "false").lower() == "true",
                # Chance to explore when eligible (0.0 - 1.0)
                "epsilon": float(os.getenv("STRATEGY_EXPLORATION_EPSILON", "0.15")),
                # Treat strategies with < N trades as "under-sampled"
                "min_trades_per_strategy": int(os.getenv("STRATEGY_EXPLORATION_MIN_TRADES", "20")),
                # Scale down position size on exploration executions
                "position_size_scale": float(os.getenv("STRATEGY_EXPLORATION_SIZE_SCALE", "1.0")),
                # If winner is only marginally stronger than runner-up, prefer exploring
                "close_strength_delta": float(os.getenv("STRATEGY_EXPLORATION_CLOSE_DELTA", "0.10")),
                # Reserve some capital for exploration trades so we can always gather sample size
                # (Normal trades size/check against available_capital * (1 - reserve_capital_pct))
                "reserve_capital_pct": float(os.getenv("STRATEGY_EXPLORATION_RESERVE_PCT", "0.10")),
            },



            # Per-strategy weight caps/floors to keep high-churn cohorts small while
            # allowing strong performers to scale. Values are fractions of total weight.
            "strategy_weight_caps": {
                # High-churn grids
                "adaptive_grid_5m": {"min": 0.05, "max": 0.25},
                "adaptive_grid_15m": {"min": 0.05, "max": 0.25},
                "adaptive_grid_1h": {"min": 0.05, "max": 0.30},
                "adaptive_grid_4h": {"min": 0.05, "max": 0.30},
                # Breakout short TF
                "vol_breakout_15m": {"min": 0.05, "max": 0.25},
                "vol_breakout_1h": {"min": 0.05, "max": 0.30},
                "vol_breakout_4h": {"min": 0.05, "max": 0.50},
                # Liquidation hunters short TF
                "liquidation_hunter_5m": {"min": 0.05, "max": 0.20},
                "liquidation_hunter_15m": {"min": 0.05, "max": 0.25},
                "liquidation_hunter_1h": {"min": 0.05, "max": 0.35},
                # Sentiment ML (keep modest, allow to grow on performance)
                "sentiment_ml_15m": {"min": 0.05, "max": 0.35},
                "sentiment_ml_1h": {"min": 0.05, "max": 0.35},
                "sentiment_ml_4h": {"min": 0.05, "max": 0.40},
                # Mean reversion/stat-arb mid/high TF
                "ou_mean_reversion_15m": {"min": 0.05, "max": 0.35},
                "ou_mean_reversion_1h": {"min": 0.05, "max": 0.40},
                "ou_mean_reversion_4h": {"min": 0.05, "max": 0.45},
                "stat_arb_15m": {"min": 0.05, "max": 0.25},
                "stat_arb_1h": {"min": 0.05, "max": 0.35},
                "stat_arb_4h": {"min": 0.05, "max": 0.50},
                # csm_4h: disabled; clean-engine re-test (2026-06) showed it was
                # a look-ahead artifact, NOT a top performer. Cap kept for the
                # entry's sake should it ever be re-tested.
                "csm_4h": {"min": 0.10, "max": 1.00},
                # Trend following (low churn by design)
                "tsmom_4h": {"min": 0.05, "max": 0.50},
            },
            # Reactive per-strategy circuit breaker: block NEW entries while a
            # strategy's realized PnL over the trailing lookback_days is below
            # -loss_threshold_pct of equity (exits unaffected; stateless — the
            # window aging out re-enables the strategy). Predictive regime
            # switching failed (2026-06); this is the reactive alternative.
            "circuit_breaker": {
                "enabled": os.getenv("CIRCUIT_BREAKER_ENABLED", "false").lower() == "true",
                "loss_threshold_pct": float(os.getenv("CIRCUIT_BREAKER_LOSS_PCT", "5.0")),
                "lookback_days": float(os.getenv("CIRCUIT_BREAKER_LOOKBACK_DAYS", "7")),
            },
            # Market-level whipsaw lockout: block new entries for lockout_days
            # after BTC prints consecutive opposite daily moves > threshold_pct
            # (crash-chop tape where both momentum and reversal lose; fired 3x
            # in Nov'25-Jun'26). See StrategyManager._whipsaw_lockout_active.
            # Portfolio gross-leverage ceiling: total notional (every leg of
            # every position, hedged or not) may not exceed this multiple of
            # equity. Backstop against leverage drift / sizing bugs — should
            # essentially never bind at current ~1-3x per-position leverage.
            "gross_leverage_cap": {
                "enabled": True,
                "max_gross_leverage": 2.0,
            },
            # Exchange-native protective stops: on entry, also rest a
            # reduce-only stop-market trigger on the exchange so the stop fires
            # even if the bot's in-process exit loop hangs or the process
            # crashes (added after the 2026-06-11 deadlock froze in-process
            # stops for ~43h). Defense in depth — the in-process realtime and
            # monitor stops still run. Live-only; the backtest mock simulates
            # it as a harmless no-op so sim fill logic is unchanged.
            "native_stop_orders": {
                "enabled": True,
            },
            # Dead man's switch (scheduleCancel): auto-cancels ALL resting
            # orders ~timeout after the bot stops heart-beating. DISABLED by
            # default: native_stop_orders are now the only resting orders, and
            # they MUST survive a crash to protect open positions — the switch
            # would cancel exactly the protection we want. Re-enable only if the
            # bot starts resting non-protective orders (e.g. ALO maker entries).
            "dead_mans_switch": {
                "enabled": False,
                "timeout_seconds": 30,
            },
            # Same-underlying concentration guard: block a new entry when an
            # open position already carries the same economic underlying
            # (dex-prefix duplicates like xyz:GOLD/cash:GOLD and tokenized
            # variants like PAXG=GOLD). See StrategyManager.UNDERLYING_ALIASES.
            "underlying_concentration": {
                "enabled": os.getenv("UNDERLYING_CONCENTRATION_GUARD", "true").lower() == "true",
            },
            # Per-strategy capital sleeves (research round 6, 2026-07-02):
            # two individually bar-passing strategies (csm_4h, sentiment_ml_1h
            # + lockout) returned -$10.1k TOGETHER vs +$21k summed solo —
            # they compete for one capital pool and evict each other's
            # positions (capital rotation churn). When enabled, each strategy
            # instance is budgeted a fixed fraction of the allocation cap and
            # capital rotation is disabled outright: a full sleeve SKIPS the
            # entry, never displaces (the validated solo profiles rotate
            # zero times — see reports/oos_matrix3). Default OFF until
            # validated by the combined matrix.
            "capital_sleeves": {
                "enabled": False,
                # Relative shares by instance name, e.g. {"csm_4h": 2.0,
                # "sentiment_ml_1h": 1.0} = 2/3 vs 1/3. Instances not listed
                # get weight 1.0; empty dict = equal split.
                "weights": {},
            },
            "whipsaw_lockout": {
                # Default ON since 2026-06-10: flips Dec-2025 positive for
                # csm_4h (+$11k single-window delta) at the cost of ~-$4k in
                # Feb; net +$8.6k over 7 windows. Threshold-insensitive
                # (2.5-3.5% identical) and duration-robust (7/14d positive).
                "enabled": os.getenv("WHIPSAW_LOCKOUT_ENABLED", "true").lower() == "true",
                "threshold_pct": float(os.getenv("WHIPSAW_LOCKOUT_THRESHOLD_PCT", "3.0")),
                "lockout_days": float(os.getenv("WHIPSAW_LOCKOUT_DAYS", "10")),
                "ref_symbol": os.getenv("WHIPSAW_LOCKOUT_REF", "BTC"),
            },
            # Cost hurdle (net edge in bps) per strategy; if a signal provides expected_edge_bps/edge_bps,
            # it must exceed this to trade. Missing edge values are ignored to stay backward compatible.
            "cost_hurdles": {
                "adaptive_grid_5m": float(os.getenv("COST_HURDLE_ADAPTIVE_GRID_5M", "5.0")),
                "adaptive_grid_15m": float(os.getenv("COST_HURDLE_ADAPTIVE_GRID_15M", "5.0")),
                "vol_breakout_15m": float(os.getenv("COST_HURDLE_VOL_BREAKOUT_15M", "6.0")),
                "vol_breakout_4h": float(os.getenv("COST_HURDLE_VOL_BREAKOUT_4H", "6.0")),
                "liquidation_hunter_5m": float(os.getenv("COST_HURDLE_LH_5M", "6.0")),
                "liquidation_hunter_15m": float(os.getenv("COST_HURDLE_LH_15M", "6.0")),
                "sentiment_ml_15m": float(os.getenv("COST_HURDLE_SENTIMENT_15M", "6.0")),
            },
        },
        
        # Leverage Management Configuration
        "leverage_management": {
            # Volatility Targeting & Risk Management
            "risk_per_trade_pct": float(os.getenv("RISK_PER_TRADE_PCT", "1.5")),  # 1.5% Risk Budget (was 3.0%)
            "target_annual_volatility": float(os.getenv("TARGET_ANNUAL_VOLATILITY", "0.40")),  # 40% Target Vol
            "fallback_stop_loss_pct": 0.08,  # Widened from 0.05 - ou_mean_reversion_15m was losing $165 on tight stops
            
            # Dynamic Leverage Constraints
            "min_leverage": float(os.getenv("LEVERAGE_MIN", "0.5")),  # Allow deleveraging for high vol
            "signal_adjustment_max": float(os.getenv("LEVERAGE_SIGNAL_ADJUSTMENT_MAX", "0.5")),
            "volatility_min": float(os.getenv("LEVERAGE_VOLATILITY_MIN", "0.1")),
            
            # Strategy Adjustments (Optional fine-tuning)
            "ma_strategy_adjustment": float(os.getenv("LEVERAGE_MA_STRATEGY_ADJUSTMENT", "1.1")),
            "rsi_strategy_adjustment": float(os.getenv("LEVERAGE_RSI_STRATEGY_ADJUSTMENT", "0.9")),
        },
        
        # Strategy Configuration
        "strategies": {
            # Dynamic Timeframe Architecture: Define specific strategy instances
            # 'instances' is the SINGLE Source of Truth for enabled strategies.
            "instances": [
                # Parallel Strategy Execution:
                # We run multiple instances of the same strategy on different timeframes.
                # The StrategySelector will dynamically weight these based on live performance.

                # Statistical Arbitrage (15m, 1h, 4h)
                # stat_arb_15m disabled due to poor backtest performance (-$40)
                # {"type": "stat_arb", "name": "stat_arb_15m", "timeframe": "15m"},
                # stat_arb_1h DISABLED per trade analysis: -$162.26 over 232 trades (47% win rate)
                # {"type": "stat_arb", "name": "stat_arb_1h",  "timeframe": "1h"},
                # stat_arb_4h DISABLED per cointegration quality check: 3 trades, 0% WR, structurally unprofitable
                # {"type": "stat_arb", "name": "stat_arb_4h",  "timeframe": "4h"},

                # OU Mean Reversion (15m, 1h, 4h)
                # ou_mean_reversion_15m DISABLED per trade analysis: -$88.50 over 505 trades (44% win rate)
                # {"type": "ou_mean_reversion", "name": "ou_mean_reversion_15m", "timeframe": "15m"},
                # ou_mean_reversion_1h DISABLED per trade analysis: -$20.44 over 59 trades (34% win rate)
                # {"type": "ou_mean_reversion", "name": "ou_mean_reversion_1h",  "timeframe": "1h"},
                # ou_mean_reversion_4h DISABLED: 3 trades, -$43 PnL, structurally unprofitable
                # {"type": "ou_mean_reversion", "name": "ou_mean_reversion_4h",  "timeframe": "4h"},
                
                # Volatility Breakout (15m, 1h, 4h)
                # vol_breakout_15m disabled due to poor performance (-$3135) - too much chop
                # {"type": "volatility_breakout", "name": "vol_breakout_15m", "timeframe": "15m"},
                # vol_breakout_1h disabled due to poor performance (23.8% win rate, -$36 PnL, -0.6% ROC)
                # 2026-06-10 re-test WITH macro trend filter, 7 windows (Nov'25-Jun'26):
                # nov -$2,469 / dec +$2,353 (single ANZ short +$4,839 carries it) /
                # jan -$1,780 / feb -$1,738 / mar +$3,110 / apr -$796 / may +$1,938.
                # Total +$618 ~ break-even; positive months broad-based but losing
                # months outnumber them. Better than vb_4h on overlapping windows,
                # still no edge. Keep off.
                # {"type": "volatility_breakout", "name": "vol_breakout_1h", "timeframe": "1h"},
                # ⚠ 2026-06 in/out-of-sample matrix (look-ahead-free engine, liquidity-
                # ranked crypto+HIP-3 universe): vol_breakout_4h is NET NEGATIVE on the
                # liquid universe even with the macro trend filter (IS -$2.8k, OOS -$2.7k;
                # filter halves the bleed but creates no edge). Its earlier +$5.2k came
                # from an accidental alphabetical mid-cap universe.
                # 2026-06-10: DISABLED — replaced by csm_4h (gate + whipsaw lockout),
                # the first configuration to pass the Dec+Apr/May validation bar.
                # Full numbers: reports/oos_matrix/SUMMARY.md
                # {"type": "volatility_breakout", "name": "vol_breakout_4h", "timeframe": "4h"},
                
                # Adaptive Grid (15m, 1h, 4h)
                # 2026-07-02 diagnostic: still 0 trades (Dec'25, May'26) —
                # entry needs ADX<30 AND RSI extreme AND ATR-band break AND
                # EMA-slope agreement at once. Inert; archive or redesign,
                # do not tune into activity.
                # {"type": "adaptive_grid", "name": "adaptive_grid_15m", "timeframe": "15m"},
                # {"type": "adaptive_grid", "name": "adaptive_grid_1h", "timeframe": "1h"},
                # {"type": "adaptive_grid", "name": "adaptive_grid_4h", "timeframe": "4h"},
                
                # Sentiment ML (15m, 1h, 4h)
                # sentiment_ml_15m disabled due to poor backtest performance (-$49)
                # {"type": "sentiment_ml", "name": "sentiment_ml_15m", "timeframe": "15m"},
                # 2026-07-02 re-test WITH whipsaw lockout (its Dec killer is
                # exactly the lockout's trigger tape): nov -$10.7k(28% DD) /
                # dec +$5.3k / jan +$7.2k / feb +$0.9k / mar +$0.8k /
                # apr +$3.9k / may +$6.6k / jun(fwd) -$1.3k = +$12.7k, PASSES
                # the validation bar SOLO. NOT enabled: run together with
                # csm_4h the pair returns -$10.1k over the same 8 windows
                # (worse than either alone in 7/8 windows — conflict
                # resolution + capital rotation churn). Capital sleeves
                # (same day) close every eviction channel and lift the
                # combined book to +$1.8k — still below csm_4h SOLO
                # (~+$9.0k/8w): residual symbol-occupancy interference +
                # halved capital. Co-running stays off; would need a
                # separate subaccount or symbol partitioning.
                # Full numbers: reports/oos_matrix3/SUMMARY.md
                # {"type": "sentiment_ml", "name": "sentiment_ml_1h", "timeframe": "1h"},
                # {"type": "sentiment_ml", "name": "sentiment_ml_4h", "timeframe": "4h"},
                
                # Liquidation Hunter (5m, 15m, 1h)
                # liquidation_hunter_5m disabled by user request (-$2k loss even with fix)
                # {"type": "liquidation_hunter", "name": "liquidation_hunter_5m", "timeframe": "5m"},
                # liquidation_hunter_15m DISABLED per trade analysis: -$55 over 74 trades (50% win rate but big losses)
                # {"type": "liquidation_hunter", "name": "liquidation_hunter_15m", "timeframe": "15m"},
                # {"type": "liquidation_hunter", "name": "liquidation_hunter_1h", "timeframe": "1h"},

                # Cross-Sectional Momentum (1h, 4h)
                # csm_1h disabled due to poor backtest performance (8.8% win rate, -$345 PnL)
                # {"type": "cross_sectional_momentum", "name": "csm_1h", "timeframe": "1h"},
                # csm_4h CONFIRMED BAD on look-ahead-free engine (2026-06 re-test):
                # Dec -$6,555 / 27% DD, Jan -$2,286 / 13% DD. Earlier "top performer"
                # label came from the biased engine. Keep disabled.
                # 2026-06-10: ENABLED with absolute-momentum gate + market-level
                # whipsaw lockout. 7-window matrix at TAKER costs (Nov'25-Jun'26):
                # nov -$6.1k / dec +$2.3k / jan +$3.2k / feb +$0.9k / mar -$4.9k /
                # apr +$6.4k / may +$7.9k = +$9.8k total. First config to pass the
                # validation bar (positive Dec-2025 AND Apr/May-2026). CAVEATS:
                # params chosen with knowledge of this 7-month period — needs
                # forward validation at small size; Nov/Mar-style months still
                # lose ~$5-6k (max DD ~18%). Maker execution (not yet built)
                # roughly doubles the expected total (ensemble mean +$22.4k).
                {"type": "cross_sectional_momentum", "name": "csm_4h", "timeframe": "4h"},

                # Trend Following (Donchian / time-series momentum)
                # 2026-06-10 IS test, top-30 universe: 60/30 bars Dec -$6.1k Feb -$2.8k;
                # 90/45 bars Dec -$11.9k Feb -$2.5k. Both sides lose (Dec shorts -$4.7k,
                # longs -$1.4k) -> no edge at either speed, long-only would not save it.
                # {"type": "trend_following", "name": "tsmom_4h", "timeframe": "4h"},

                # Cross-Sectional Funding Carry (funding as directional signal)
                # 2026-07-03: NEW, backtesting in progress — enable via
                # --enable-instance funding_carry:funding_carry_1h:1h
                # {"type": "funding_carry", "name": "funding_carry_1h", "timeframe": "1h"},

                # NOTE (2026-06 clean-engine re-test of vol_breakout_1h): only
                # profitable in the Dec-2025 crash regime (+$6k, concentrated in
                # one ANZ short); Nov/Jan/Feb all negative (PF 0.5-0.8). Keep off.
            ],
            # Note: timeframe is now auto-selected per strategy instance
            "ohlcv_limit": int(os.getenv("OHLCV_LIMIT", "300")), # Increased for EMA200
            "stat_arb": {
                "z_score_threshold": float(os.getenv("STAT_ARB_Z_SCORE_THRESHOLD", "2.0")),
                "window_size": int(os.getenv("STAT_ARB_WINDOW_SIZE", "100")),
                "min_correlation": float(os.getenv("STAT_ARB_MIN_CORRELATION", "0.8")),
                "correlation_lookback": int(os.getenv("STAT_ARB_CORRELATION_LOOKBACK", "100")),
                "update_interval_hours": int(os.getenv("STAT_ARB_UPDATE_INTERVAL_HOURS", "24")),
                "zscore_hurdle_buffer": 0.0, # Strategy-specific hurdle buffer (0.0 = disabled/neutral)
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
                "zscore_hurdle_buffer": 0.3, # Specific buffer for mean reversion
            },
            

            
            # Volatility Breakout Strategy
            "volatility_breakout": {
                "bb_length": int(os.getenv("VB_BB_LENGTH", "20")),
                "bb_std": float(os.getenv("VB_BB_STD", "2.0")),
                # Percentile squeeze: bandwidth must be in the lowest X of its own
                # trailing distribution (asset-relative, unlike the absolute threshold)
                "squeeze_percentile": float(os.getenv("VB_SQUEEZE_PERCENTILE", "0.20")),
                "squeeze_window": int(os.getenv("VB_SQUEEZE_WINDOW", "100")),
                # Absolute fallback while bandwidth history is short
                "squeeze_threshold": float(os.getenv("VB_SQUEEZE_THRESHOLD", "0.15")), # Increased from 0.10
                # Volume confirmation on the breakout candle
                "volume_mult": float(os.getenv("VB_VOLUME_MULT", "1.5")),
                "volume_lookback": int(os.getenv("VB_VOLUME_LOOKBACK", "20")),
                "atr_length": int(os.getenv("VB_ATR_LENGTH", "14")),
                "atr_multiplier_sl": float(os.getenv("VB_ATR_MULT_SL", "1.5")), # Tighter stops (was 2.0)
                "atr_multiplier_tp": float(os.getenv("VB_ATR_MULT_TP", "4.0")),
                # Chandelier-style trailing stop (ATR units, converted to pct at entry)
                "trail_atr_mult": float(os.getenv("VB_TRAIL_ATR_MULT", "2.5")),
                "trail_activation_atr_mult": float(os.getenv("VB_TRAIL_ACTIVATION_ATR_MULT", "1.0")),
                # Macro trend filter: no shorts above / longs below the long EMA.
                # Counter-trend breakouts were the dominant loss source on
                # liquid symbols (2026-06 OOS matrix).
                "trend_filter_enabled": os.getenv("VB_TREND_FILTER", "true").lower() == "true",
                "trend_ema_period": int(os.getenv("VB_TREND_EMA_PERIOD", "200")),
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
                "adx_threshold": 25,
                # Dual-momentum gate: rank alone can long assets that are merely
                # falling slowest (Dec-2025: -$14.6k, 32% DD). 1 = require the
                # asset's own return to point the same way as the trade.
                # Default ON since 2026-06-10 (validated config, see instances).
                "require_absolute_momentum": int(os.getenv("CSM_REQUIRE_ABS_MOMENTUM", "1")),
                "min_abs_momentum": 0.0,
                "stop_loss_pct": 0.05,
                # >0: ATR-scaled stop (entry +/- mult*ATR14); engine sizes off
                # the stop distance, so wider vol-aware stops = smaller size
                "stop_atr_mult": 0.0,
                # Rank on the window ending N bars ago (12-2-style momentum)
                "skip_period": 0,
                # 1 = short-term REVERSAL mode: buy bottom decile, fade top
                # (abs-momentum gate flips with it)
                "invert": 0,
                # EMA200 alignment filter (momentum construct; 0 for reversal)
                "trend_filter_enabled": 1,
            },

            # Cross-Sectional Funding Carry (research round 7, 2026-07-03)
            # Funding as a DIRECTIONAL signal: short the most crowded longs
            # (top decile trailing funding — they pay hourly to hold), long
            # the most crowded shorts; receive the carry while positioned.
            # One leg per name — deliberately avoids the 4-leg fee math that
            # killed delta-neutral funding_rate_arbitrage.
            "funding_carry": {
                "funding_lookback_hours": 24,   # trailing window for the signal
                "exit_lookback_hours": 8,       # short window for the funding-flip exit
                "top_n_percent": 0.15,          # fade top/bottom 15% of the universe
                # HL baseline funding ~11% APR: the gate must sit well above
                # it (v0 at 10% churned 94 trades/week on normal names)
                "min_abs_funding_apr": 0.30,
                "min_universe": 10,
                "stop_loss_pct": 0.05,
                "exit_apr_buffer": 0.05,        # flip must clearly cross, not touch zero
                "min_holding_hours": 8,         # no flip-exit while young; stops still protect
            },

            # Trend Following (Donchian / time-series momentum)
            "trend_following": {
                "entry_lookback": int(os.getenv("TF_ENTRY_LOOKBACK", "60")),   # 60 x 4h = 10 days
                "exit_lookback": int(os.getenv("TF_EXIT_LOOKBACK", "30")),     # 30 x 4h = 5 days
                "atr_length": int(os.getenv("TF_ATR_LENGTH", "14")),
                "atr_multiplier_sl": float(os.getenv("TF_ATR_MULT_SL", "2.0")),
                "trail_atr_mult": float(os.getenv("TF_TRAIL_ATR_MULT", "3.0")),
                "trail_activation_atr_mult": float(os.getenv("TF_TRAIL_ACTIVATION_ATR_MULT", "1.5")),
                "direction": os.getenv("TF_DIRECTION", "both"),
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
            "initial_capital": float(os.getenv("BACKTEST_INITIAL_CAPITAL", "50000.0")),
            "initial_spot_balance": float(os.getenv("BACKTEST_INITIAL_SPOT_BALANCE", "0.0")),
            # Execution cost model per leg (see MockMarketAPI). Defaults
            # approximate taker execution; maker-scenario upper bound:
            # BACKTEST_FEE_BPS=1.5 BACKTEST_SLIPPAGE_BPS=0
            "fee_bps": float(os.getenv("BACKTEST_FEE_BPS", "5.0")),
            "slippage_bps": float(os.getenv("BACKTEST_SLIPPAGE_BPS", "5.0")),
            # Post-only entry model (see MockMarketAPI): entries fill with
            # fill_prob at the limit (maker fee, no slippage) or are missed;
            # exits always pay taker. Deterministic via seed.
            "maker_execution": {
                "enabled": os.getenv("BACKTEST_MAKER_EXECUTION", "false").lower() == "true",
                "fill_prob": float(os.getenv("BACKTEST_MAKER_FILL_PROB", "0.75")),
                "maker_fee_bps": float(os.getenv("BACKTEST_MAKER_FEE_BPS", "1.5")),
                "seed": int(os.getenv("BACKTEST_MAKER_SEED", "42")),
            },
        },
        
        # Order Management Configuration
        "order_management": {
            "status_timeout": int(os.getenv("ORDER_STATUS_TIMEOUT", "30")),
        },
        
        # System Configuration
        "system": {
            # Whether to close all positions on shutdown (default: False for upgrade safety)
            "close_on_shutdown": os.getenv("CLOSE_ON_SHUTDOWN", "false").lower() == "true",
        },
        
        
        # Data Collection Configuration
        "data_collection": {
            "price_history_max_length": int(os.getenv("PRICE_HISTORY_MAX_LENGTH", "1000")),
            "ohlcv_history_max_length": int(os.getenv("OHLCV_HISTORY_MAX_LENGTH", "1000")),
            # Startup Integrity Check (Disable by default to prioritize trading startup speed and avoid rate limits)
            "enable_integrity_check": os.getenv("ENABLE_INTEGRITY_CHECK", "false").lower() == "true",
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