"""
Strategy manager for orchestrating trading strategies.
"""

import logging
import time
import signal
import sys
import json
import random
from typing import Dict, List, Any, Optional
from datetime import datetime, timedelta
import pandas as pd
import math

from src.api import HyperliquidAPI
from src.utils.pair_selector import DynamicPairSelector
from src.utils.leverage_manager import LeverageManager
from src.utils.portfolio_manager import PortfolioManager
from src.utils.correlation_manager import CorrelationManager
from src.utils.performance_tracker import PerformanceTracker
from src.utils.regime_hmm import RegimeAllocator
from src.utils.change_point import PageHinkley
from .strategy_selector import StrategySelector
from .execution_engine import ExecutionEngine
from src.models.trade import Trade, Position, MultiLegPosition, PositionLeg

# Strategy imports - only used when enabled in config
STRATEGY_CLASSES = {
    # Legacy strategies (kept for experimentation/backwards-compat)
    'moving_average': ('legacy.moving_average_strategy', 'MovingAverageStrategy'),
    'rsi': ('legacy.rsi_strategy', 'RSIStrategy'),
    'bollinger_band': ('legacy.bollinger_band_strategy', 'BollingerBandSqueezeStrategy'),
    'supertrend': ('legacy.supertrend_strategy', 'SupertrendStrategy'),
    'vwap': ('legacy.vwap_strategy', 'VWAPStrategy'),
    'stat_arb': ('statistical_arbitrage_strategy', 'StatisticalArbitrageStrategy'),
    'funding_rate_arbitrage': ('funding_rate_arbitrage_strategy', 'FundingRateArbitrageStrategy'),
    'ou_mean_reversion': ('ou_mean_reversion_strategy', 'OUMeanReversionStrategy'),
    'momentum_factor': ('momentum_factor_strategy', 'MomentumFactorStrategy'),
}


class StrategyManager:
    """Manages and orchestrates trading strategies."""
    
    def __init__(self, config: Dict[str, Any], market_api: Any = None):
        """
        Initialize the strategy manager.
        
        Args:
            config: Configuration dictionary
            market_api: Optional injected market API (for testing)
        """
        self.config = config
        self.logger = logging.getLogger(__name__)

        # Session tracking (used by dashboard to compute "since bot started" metrics)
        self.session_start_time = datetime.now()
        
        # Initialize portfolio manager
        self.portfolio_manager = PortfolioManager(config)
        
        # Initialize leverage manager with portfolio manager
        self.leverage_manager = LeverageManager(config, self.portfolio_manager)
        
        # Initialize market API
        self.market_api = market_api if market_api else self._initialize_market_api()
        
        # Initialize correlation manager
        self.correlation_manager = CorrelationManager(self.market_api, config)
        
        # Initialize performance tracker
        self.performance_tracker = PerformanceTracker(config, data_dir='data')
        
        # Initialize strategies
        self.strategies = self._initialize_strategies()
        
        # Initialize strategy selector for performance-based selection
        self.strategy_selector = self._initialize_strategy_selector()
        
        # Initialize pair selector
        self.pair_selector = self._initialize_pair_selector()
        
        # Trading configuration
        # Note: timeframe is now per-strategy, not global
        self.ohlcv_limit = config['strategies']['ohlcv_limit']
        self.max_positions_percentage = config['trading']['max_positions_percentage']
        self.base_currency = config['trading']['base_currency']
        
        # Order monitoring configuration
        self.order_timeout_minutes = config['trading']['order_timeout_minutes']
        self.enable_stale_order_cleanup = config['trading']['enable_stale_order_cleanup']
        self.position_sync_interval = config['trading']['position_sync_interval']
        self.enable_position_validation = config['trading']['enable_position_validation']
        
        # Trading pairs management
        self.max_pairs_to_trade = 20  # Default value, can be updated dynamically
        
        # Calculate execution interval based on timeframe
        self.execution_interval = self._get_execution_interval()
        
        # Initialize Execution Engine
        self.execution_engine = ExecutionEngine(
            self.config,
            self.market_api,
            self.leverage_manager,
            self.portfolio_manager,
            self.performance_tracker,
            self.pair_selector
        )
        
        self.is_running = False
        
        self.logger.info(f"Initialized strategy manager with {len(self.strategies)} strategies")
        for name, strat in self.strategies.items():
            self.logger.info(f"  {name}: timeframe={strat.timeframe}")
        self.logger.info(f"Execution interval: {self.execution_interval}s (based on fastest strategy)")
        self.logger.info(f"Position limit: {self.max_positions_percentage}% of portfolio")
        self.logger.info("Dynamic leverage management enabled")
        
        # Set strategy manager reference in pair selector
        self.pair_selector.strategy_manager = self
        
        # Real-time data subscription tracking
        self._subscribed_symbols = set()
        
        # Real-time price monitoring
        self._price_callbacks = []
        self._last_prices = {}
        
        # Signal handling and shutdown safety is managed by the top-level entrypoint (`src/main.py`).

        # Regime + change-point gating
        self._regime_allocator: Optional[RegimeAllocator] = None
        self._regime_result: Optional[Dict[str, Any]] = None
        self._regime_last_update_ts: float = 0.0

        self._change_point: Optional[PageHinkley] = None
        self._entry_block_until: Optional[datetime] = None
        self._entry_block_reason: Optional[str] = None
        self._entry_block_strategies = set()

        self._initialize_regime_and_changepoint()

        # Cycle trackers for run_trading_cycle
        self.last_position_sync = 0
        self.last_position_monitoring = 0
        self.last_performance_report = 0
        self.total_positions_closed = 0
        self.emergency_stops_triggered = 0
        self.last_emergency_check = 0

    @property
    def positions(self):
        return self.execution_engine.positions

    @property
    def multi_leg_positions(self):
        return self.execution_engine.multi_leg_positions
        
    @property
    def trades(self):
        return self.execution_engine.trades
        
    @property
    def total_trades(self):
        return self.execution_engine.total_trades
        
    @property
    def total_pnl(self):
        return self.execution_engine.total_pnl
        
    @property
    def winning_trades(self):
        return self.execution_engine.winning_trades

    def _initialize_regime_and_changepoint(self) -> None:
        """Initialize optional regime allocator and change-point detector."""
        rm = self.config.get("risk_management", {})

        ra = rm.get("regime_allocator", {}) or {}
        if ra.get("enabled", False):
            try:
                self._regime_allocator = RegimeAllocator(
                    lookback=int(ra.get("lookback", 220)),
                    retrain_minutes=int(ra.get("retrain_minutes", 30)),
                    hysteresis_threshold=float(ra.get("hysteresis_threshold", 0.60)),
                    min_switch_minutes=int(ra.get("min_switch_minutes", 15)),
                )
                self.logger.info(
                    "Regime allocator enabled (3-state HMM): "
                    f"proxy={ra.get('proxy_symbol','BTC')}/{ra.get('timeframe','15m')}, "
                    f"lookback={ra.get('lookback',220)}, retrain={ra.get('retrain_minutes',30)}m"
                )
            except Exception as e:
                self._regime_allocator = None
                self.logger.warning(f"Failed to initialize regime allocator; disabled. Error: {e}")

        cp = rm.get("change_point", {}) or {}
        if cp.get("enabled", False):
            try:
                self._change_point = PageHinkley(
                    delta=float(cp.get("delta", 0.0)),
                    threshold=float(cp.get("threshold", 0.02)),
                    alpha=float(cp.get("alpha", 0.99)),
                )
                self._entry_block_strategies = set(cp.get("apply_to_strategies", []) or [])
                self.logger.info(
                    "Change-point gate enabled (Page-Hinkley): "
                    f"proxy={cp.get('proxy_symbol','BTC')}/{cp.get('timeframe','15m')}, "
                    f"cooldown={cp.get('cooldown_minutes',20)}m, "
                    f"strategies={sorted(self._entry_block_strategies)}"
                )
            except Exception as e:
                self._change_point = None
                self.logger.warning(f"Failed to initialize change-point detector; disabled. Error: {e}")

    def _is_entry_block_active(self) -> bool:
        return self._entry_block_until is not None and datetime.now() < self._entry_block_until

    def _is_strategy_entry_blocked(self, strategy_name: str) -> bool:
        return self._is_entry_block_active() and strategy_name in self._entry_block_strategies

    def _get_effective_strategy_weight(self, strategy_name: str) -> float:
        """Selector weight multiplied by regime multiplier (if enabled)."""
        base_weight = float(self.strategy_selector.get_strategy_weight(strategy_name))
        if not self._regime_allocator or not self._regime_result:
            return base_weight
        regime = str(self._regime_result.get("regime", "range"))
        mult = float(self._regime_allocator.get_multiplier(strategy_name, regime))
        return max(0.0, base_weight * mult)

    def _maybe_update_regime_and_changepoint(self) -> None:
        """Update regime and change-point state from the configured proxy symbol."""
        rm = self.config.get("risk_management", {})
        ra = rm.get("regime_allocator", {}) or {}
        cp = rm.get("change_point", {}) or {}

        # We update at most once per execution cycle.
        now_ts = time.time()
        if self._regime_last_update_ts and (now_ts - self._regime_last_update_ts) < max(5.0, float(self.execution_interval)):
            return
        self._regime_last_update_ts = now_ts

        proxy_symbol = ra.get("proxy_symbol") or cp.get("proxy_symbol") or "BTC"
        timeframe = ra.get("timeframe") or cp.get("timeframe") or "15m"
        lookback = int(ra.get("lookback", 220))

        try:
            df = self.market_api.get_ohlcv(proxy_symbol, timeframe, limit=max(lookback, 120))
            if df is None or len(df) < 50:
                return

            # Update regime allocator
            if self._regime_allocator and ra.get("enabled", False):
                X = self._regime_allocator.build_features_from_ohlcv(df)
                res = self._regime_allocator.update(X, now_ts)
                self._regime_result = {"regime": res.regime, "probs": res.probs}
                self.logger.debug(f"Regime={res.regime} probs={res.probs}")

            # Update change-point detector on abs return of last bar
            if self._change_point and cp.get("enabled", False):
                close = df["close"].astype(float).values
                if len(close) >= 2 and close[-2] > 0:
                    r = abs((close[-1] - close[-2]) / close[-2])
                    triggered, score = self._change_point.update(float(r))
                    if triggered:
                        cooldown = int(cp.get("cooldown_minutes", 20))
                        self._entry_block_until = datetime.now() + timedelta(minutes=cooldown)
                        self._entry_block_reason = f"change_point(score={score:.4f})"
                        self.logger.warning(
                            f"⚠️ Change-point detected on {proxy_symbol} ({timeframe}): score={score:.4f}. "
                            f"Blocking new entries for {sorted(self._entry_block_strategies)} until {self._entry_block_until}."
                        )
                        # Reset detector so we don't immediately retrigger in the cooldown window
                        self._change_point.reset()

        except Exception as e:
            self.logger.debug(f"Regime/changepoint update failed: {e}")
    
    def _update_account_balance(self):
        """Update account balance and portfolio information."""
        try:
            # Update portfolio information
            if self.portfolio_manager.should_update_portfolio():
                success = self.portfolio_manager.update_portfolio_info(self.market_api)
                if success:
                    # Update leverage manager with new portfolio info
                    self.leverage_manager.update_available_margin(
                        self.portfolio_manager.calculate_available_capital_for_trading()
                    )
                    
                    # Log portfolio summary
                    portfolio_summary = self.portfolio_manager.get_portfolio_summary()
                    self.logger.info(f"Portfolio updated: ${portfolio_summary['total_equity']:.2f} total equity, "
                                   f"${portfolio_summary['available_capital']:.2f} available for trading")
                else:
                    self.logger.warning("Failed to update portfolio information")
                    
        except Exception as e:
            self.logger.error(f"Error updating account balance: {e}")
    
    def _get_execution_interval(self) -> int:
        """
        Get execution interval for strategy checking.
        
        Uses a fixed 15-second interval to ensure strategies can react
        quickly to opportunities. OHLCV data is continuously updated
        via WebSocket, so frequent checks don't require additional API calls.
        
        Returns:
            Execution interval in seconds
        """
        # Fixed 15-second interval for responsive opportunity detection
        # WebSocket keeps price data updated in real-time
        return 15
    
    
    def _calculate_market_volatility(self, ohlcv: pd.DataFrame) -> float:
        """
        Calculate market volatility measure.
        
        Args:
            ohlcv: OHLCV data
            
        Returns:
            Volatility measure (0-1)
        """
        try:
            if len(ohlcv) < 20:
                return 0.5
            
            # Calculate price volatility
            returns = ohlcv['close'].pct_change().dropna()
            volatility = returns.std() * math.sqrt(252)  # Annualized volatility
            
            # Normalize to 0-1 range (assuming max 100% annualized volatility)
            normalized_volatility = min(1.0, volatility)
            
            return normalized_volatility
            
        except Exception as e:
            self.logger.error(f"Error calculating volatility: {e}")
            return 0.5
    
    def _initialize_market_api(self):
        """Initialize the market API client."""
        # Use unified HyperliquidAPI with built-in WebSocket and REST support
        return HyperliquidAPI(self.config)
    
    def _initialize_strategies(self):
        """Initialize only enabled trading strategies."""
        import importlib
        
        enabled_strategies = self.config['strategies']['enabled']
        strategies = {}
        
        for strategy_name in enabled_strategies:
            strategy_name = strategy_name.strip()
            if strategy_name not in STRATEGY_CLASSES:
                self.logger.warning(f"Unknown strategy: {strategy_name}")
                continue
            
            module_name, class_name = STRATEGY_CLASSES[strategy_name]
            
            try:
                # Dynamically import the strategy module
                module = importlib.import_module(f'.{module_name}', package='src.strategies')
                strategy_class = getattr(module, class_name)
                
                # Some strategies require additional arguments
                if strategy_name == 'stat_arb':
                    strategies[strategy_name] = strategy_class(
                        self.config, self.market_api, self.correlation_manager
                    )
                elif strategy_name in ('funding_rate_arbitrage', 'momentum_factor'):
                    # These strategies accept optional market_api
                    strategies[strategy_name] = strategy_class(
                        self.config, self.market_api
                    )
                else:
                    # Standard strategies only take config
                    strategies[strategy_name] = strategy_class(self.config)
                
                self.logger.info(f"Initialized strategy: {strategy_name}")
                
            except Exception as e:
                self.logger.error(f"Failed to initialize strategy {strategy_name}: {e}")
        
        return strategies
    
    def _initialize_strategy_selector(self):
        """Initialize the strategy selector for automatic performance-based selection."""
        # Pass strategy names so they can be enabled by default
        strategy_names = list(self.strategies.keys())
        return StrategySelector(
            performance_tracker=self.performance_tracker,
            config=self.config,
            strategy_names=strategy_names,
        )
    
    def _initialize_pair_selector(self):
        """Initialize the pair selector."""
        return DynamicPairSelector(self.config, self.market_api, self)
    
    def start(self, enable_dashboard: bool = True, dashboard_port: int = 5050):
        """
        Start the strategy manager.
        
        Args:
            enable_dashboard: Whether to start the web dashboard (default: True)
            dashboard_port: Port for the dashboard server (default: 5050)
        """
        if self.is_running:
            self.logger.warning("Strategy manager is already running")
            return
        
        self.logger.info("Starting strategy manager...")
        
        # Start dashboard if enabled
        if enable_dashboard:
            try:
                from src.dashboard import run_dashboard
                self._dashboard_thread = run_dashboard(
                    strategy_manager=self,
                    port=dashboard_port
                )
                self.logger.info(f"Dashboard available at http://localhost:{dashboard_port}")
            except ImportError as e:
                self.logger.warning(f"Dashboard not available (run 'pip install flask'): {e}")
            except Exception as e:
                self.logger.warning(f"Failed to start dashboard: {e}")
        
        # Start market API (only if it has a start method)
        if hasattr(self.market_api, 'start'):
            if not self.market_api.start():
                self.logger.error("Failed to start market API")
                return
        
        # Setup real-time price monitoring
        if hasattr(self.market_api, 'add_price_callback'):
            self.market_api.add_price_callback(self._on_price_update)
            self.logger.info("Real-time price monitoring enabled")
        
        # Setup real-time position monitoring
        if hasattr(self.market_api, 'add_position_callback'):
            self.market_api.add_position_callback(self._on_position_update)
            self.logger.info("Real-time position monitoring enabled")
        
        # Initial portfolio update
        self._update_account_balance()
        
        # Update available margin
        self.leverage_manager.update_available_margin(
            self.portfolio_manager.calculate_available_capital_for_trading()
        )
        
        # Initialize performance tracker with current equity
        initial_equity = self.portfolio_manager.calculate_available_capital_for_trading()
        self.performance_tracker.set_initial_equity(initial_equity)
        self.logger.info(f"Performance tracker initialized with ${initial_equity:.2f} initial equity")
        
        # Start trading loop
        self.is_running = True
        self._run_trading_loop()
    
    def _update_account_balance_periodic(self):
        """Periodically update account balance."""
        try:
            balance_info = self.market_api.get_account_balance()
            if balance_info:
                old_capital = self.portfolio_manager.calculate_available_capital_for_trading()
                self.portfolio_manager.update_portfolio_info(self.market_api)
                
                # Only log if there's a significant change
                if abs(self.portfolio_manager.calculate_available_capital_for_trading() - old_capital) > 1.0:
                    self.logger.info(f"Account balance updated: ${self.portfolio_manager.calculate_available_capital_for_trading():.2f} (change: ${self.portfolio_manager.calculate_available_capital_for_trading() - old_capital:+.2f})")
                
                # Update leverage manager with new capital
                self.leverage_manager.update_available_margin(self.portfolio_manager.calculate_available_capital_for_trading())
        except Exception as e:
            self.logger.error(f"Error updating account balance: {e}")
    
    def stop(self, close_positions: bool = False):
        """
        Stop the strategy manager.
        
        Args:
            close_positions: Whether to close all positions before stopping
        """
        if not self.is_running:
            return
        
        self.logger.info("Stopping strategy manager...")
        self.is_running = False
        
        if close_positions:
            self.logger.warning("Closing all positions before stopping...")
            try:
                self.sync_positions_with_exchange()
                self.close_all_positions("shutdown")
            except Exception as e:
                self.logger.error(f"Error closing positions during stop: {e}")
        
        # Stop market API (only if it has a stop method)
        if hasattr(self.market_api, 'stop'):
            self.market_api.stop()
        
        self.logger.info("Strategy manager stopped")
    
    def emergency_stop(self):
        """Emergency stop - close all positions and stop trading."""
        self.logger.warning("EMERGENCY STOP: Closing all positions...")
        self.stop(close_positions=True)
    
    def sync_positions_with_exchange(self):
        """
        Synchronize local positions with actual exchange positions.
        This ensures accuracy by comparing local state with exchange state.
        """
        try:
            # Get actual positions from exchange
            exchange_positions = self.market_api.get_positions()
            self.logger.debug(f"Exchange positions: {len(exchange_positions)}")
            exchange_position_symbols = {pos['symbol'] for pos in exchange_positions}
            
            # Get local position symbols
            local_position_symbols = set(self.positions.keys())
            
            # Find positions that exist locally but not on exchange (closed positions)
            closed_positions = local_position_symbols - exchange_position_symbols
            for symbol in closed_positions:
                self.logger.info(f"Position {symbol} no longer exists on exchange, removing from local state")
                if symbol in self.positions:
                    del self.positions[symbol]
            
            # Find positions that exist on exchange but not locally (new positions)
            new_positions = exchange_position_symbols - local_position_symbols
            for symbol in new_positions:
                exchange_pos = next(pos for pos in exchange_positions if pos['symbol'] == symbol)
                self.logger.info(f"Found new position {symbol} on exchange, adding to local state")
                
                # Create new position object
                position = Position(
                    symbol=symbol,
                    side=exchange_pos['side'],
                    size=abs(exchange_pos['size']),
                    entry_price=exchange_pos['entry_price'],
                    entry_time=datetime.now(),  # We don't have exact entry time from exchange
                    strategy='unknown',  # We don't know which strategy opened this
                    current_price=exchange_pos['mark_price']
                )
                self.positions[symbol] = position
            
            # Update existing positions with current exchange data
            for exchange_pos in exchange_positions:
                symbol = exchange_pos['symbol']
                if symbol in self.positions:
                    local_pos = self.positions[symbol]
                    # Update current price and size
                    local_pos.current_price = exchange_pos['mark_price']
                    local_pos.size = abs(exchange_pos['size'])
                    
                    # Check for significant discrepancies
                    size_diff = abs(local_pos.size - abs(exchange_pos['size']))
                    if size_diff > 0.001:  # Allow for small floating point differences
                        self.logger.warning(f"Size discrepancy for {symbol}: local={local_pos.size}, exchange={exchange_pos['size']}")
                        local_pos.size = abs(exchange_pos['size'])
            
            # If there are local positions but no exchange positions, clear local positions
            # This handles the case where orders were placed but not actually filled
            if len(exchange_positions) == 0 and len(self.positions) > 0:
                self.logger.warning(f"No exchange positions found but {len(self.positions)} local positions exist - clearing local positions")
                self.positions.clear()
            
            # Save updated positions to file
            self.save_positions_to_file()
            
            self.logger.info(f"Position sync complete: {len(self.positions)} local positions, {len(exchange_positions)} exchange positions")
            
        except Exception as e:
            self.logger.error(f"Error syncing positions with exchange: {e}")

    def _cleanup_stale_orders(self):
        """
        Clean up stale orders that have been open for too long.
        This helps prevent orders from getting stuck.
        """
        if not self.enable_stale_order_cleanup:
            return
            
        try:
            open_orders = self.market_api.get_open_orders()
            
            if not open_orders:
                return
            
            current_time = datetime.now()
            stale_orders = []
            
            for order in open_orders:
                # Check if order is older than configured timeout (in minutes, converted to seconds)
                order_timestamp = order.get('timestamp', 0)
                if order_timestamp:
                    order_time = datetime.fromtimestamp(order_timestamp / 1000)  # Convert from milliseconds
                    time_diff = (current_time - order_time).total_seconds()  # Seconds
                    timeout_seconds = self.order_timeout_minutes * 60  # Convert minutes to seconds
                    
                    if time_diff > timeout_seconds:
                        stale_orders.append(order)
                        self.logger.warning(f"Stale order detected: {order['symbol']} {order['side']} {order['size']} @ {order['price']} (age: {time_diff:.1f} seconds)")
            
            # Cancel stale orders
            for order in stale_orders:
                try:
                    order_id = order.get('order_id')
                    symbol = order.get('symbol')
                    if order_id and symbol:
                        success = self.market_api.cancel_order(symbol, order_id)
                        if success:
                            self.logger.info(f"Cancelled stale order: {order['symbol']} (ID: {order_id})")
                        else:
                            self.logger.error(f"Failed to cancel stale order: {order['symbol']} (ID: {order_id})")
                except Exception as e:
                    self.logger.error(f"Error cancelling stale order {order.get('symbol', 'unknown')}: {e}")
            
            if stale_orders:
                self.logger.info(f"Cleaned up {len(stale_orders)} stale orders")
                
        except Exception as e:
            self.logger.error(f"Error cleaning up stale orders: {e}")

    def _monitor_pending_orders(self):
        """Monitor any pending orders and update their status."""
        try:
            open_orders = self.market_api.get_open_orders()
            
            if open_orders:
                self.logger.info(f"Monitoring {len(open_orders)} open orders...")
                
                for order in open_orders:
                    order_id = order.get('order_id')
                    symbol = order.get('symbol')
                    side = order.get('side')
                    size = order.get('size')
                    price = order.get('price')
                    
                    # Check if order is still valid (not too old)
                    # For now, we'll just log the status
                    self.logger.info(f"Open order: {symbol} {side} {size} @ {price} (ID: {order_id})")
                    
                            # Order timeout and cancellation logic implemented in _cleanup_stale_orders
            # No open orders to monitor
                    
        except Exception as e:
            self.logger.error(f"Error monitoring pending orders: {e}")

    def run_trading_cycle(self) -> bool:
        """
        Run a single iteration of the trading logic.
        
        Returns:
            bool: True if trading logic ran (pairs analyzed), False if skipped (no pairs)
        """
        try:
            current_time = time.time()

            # Update regime and change-point gating from market proxy (once per cycle)
            self._maybe_update_regime_and_changepoint()
            
            # With WebSocket, positions are updated in real-time
            # Only sync periodically to ensure accuracy
            if current_time - self.last_position_sync >= self.position_sync_interval:
                self.logger.debug(f"Syncing positions with exchange (local: {len(self.positions)})")
                self.sync_positions_with_exchange()
                self.last_position_sync = current_time
            
            # Check liquidation risks for multi-leg positions
            self._check_liquidation_risks()
            
            # Continuous position monitoring and auto-closure
            position_monitoring_interval = self.config['trading']['position_monitoring_interval']
            emergency_portfolio_loss_pct = self.config.get('risk_management', {}).get('emergency_portfolio_loss_pct', 10.0)
            
            if current_time - self.last_position_monitoring >= position_monitoring_interval:
                self.logger.debug(f"Running position monitoring check ({len(self.positions)} positions)")
                self._monitor_and_close_positions(
                    emergency_portfolio_loss_pct, 
                    self.total_positions_closed, 
                    self.emergency_stops_triggered, 
                    self.last_emergency_check
                )
                self.last_position_monitoring = current_time
            
            # Validate position integrity (if enabled)
            if self.enable_position_validation:
                validation_results = self.validate_position_integrity()
                if validation_results['total_issues'] > 0:
                    self.logger.error(f"Position validation found {validation_results['total_issues']} critical issues")
            
            # Monitor any pending orders
            self._monitor_pending_orders()
            
            # Clean up stale orders
            self._cleanup_stale_orders()
            
            # Update correlations periodically
            if self.correlation_manager.should_update():
                # Get all potential symbols from pair selector or config
                all_symbols = self.pair_selector.get_current_pairs()
                if all_symbols:
                    self.correlation_manager.update_correlations(all_symbols)
            
            # Get current trading pairs
            trading_pairs = self.pair_selector.get_current_pairs()
            
            if not trading_pairs:
                self.logger.warning("No trading pairs available")
                return False
            
            self.logger.info(f"Analyzing {len(trading_pairs)} trading pairs")
            
            # Analyze each symbol (per-symbol strategies)
            for symbol in trading_pairs:
                # Check running flag inside loop to support clean shutdown
                if not self.is_running:
                    break
                self._analyze_symbol(symbol)
            
            # Run cross-sectional strategies (portfolio-level)
            self._run_portfolio_strategies(trading_pairs)
            
            # Update position prices and display PnL
            self.update_position_prices()
            self.display_positions_pnl()
            
            # Periodically update account balance (every 10 cycles)
            if hasattr(self, '_balance_update_counter'):
                self._balance_update_counter += 1
            else:
                self._balance_update_counter = 0
            
            if self._balance_update_counter >= 10:
                self._update_account_balance_periodic()
                self._balance_update_counter = 0
            
            # Periodic performance report (every hour)
            performance_report_interval = 3600
            if current_time - self.last_performance_report >= performance_report_interval:
                self.log_performance_report()
                self.last_performance_report = current_time
                
            return True
            
        except Exception as e:
            self.logger.error(f"Error in trading cycle: {e}")
            return False

    def _run_trading_loop(self):
        """Main trading loop."""
        self.logger.info("Starting trading loop...")
        
        while self.is_running:
            try:
                # Run one cycle of trading logic
                # This has been extracted to allow step-by-step execution in backtesting
                self.run_trading_cycle()
                
                # Wait for next execution cycle
                # We always sleep here to throttle the loop and mimic interval-based execution
                time.sleep(self.execution_interval)
                
            except KeyboardInterrupt:
                self.logger.info("Received interrupt signal")
                break
            except Exception as e:
                self.logger.error(f"Error in trading loop: {e}")
                time.sleep(self.execution_interval)
        
        self.logger.info("Trading loop stopped")
    
    def _subscribe_to_symbol(self, symbol: str):
        """Subscribe to real-time data for a symbol."""
        if symbol not in self._subscribed_symbols:
            if hasattr(self.market_api, 'subscribe_symbol'):
                self.market_api.subscribe_symbol(symbol)
                self._subscribed_symbols.add(symbol)
                self.logger.info(f"Subscribed to real-time data for {symbol}")
    
    def _on_price_update(self, symbol: str, price: float, timestamp: float):
        """Handle real-time price updates."""
        old_price = self._last_prices.get(symbol)
        self._last_prices[symbol] = price
        
        # Update rolling OHLCV cache in market_api from tick
        try:
            self.market_api.update_ohlcv_from_tick(symbol, price, volume=0.0, ts=timestamp)
        except Exception as e:
            self.logger.debug(f"OHLCV tick update error for {symbol}: {e}")
        
        if old_price is not None:
            price_change = ((price - old_price) / old_price) * 100
            if abs(price_change) > 1.0:  # Log significant price changes (>1%)
                self.logger.info(f"Real-time price update for {symbol}: ${old_price:.4f} → ${price:.4f} ({price_change:+.2f}%)")
        
        # Notify callbacks
        for callback in self._price_callbacks:
            try:
                callback(symbol, price, timestamp)
            except Exception as e:
                self.logger.error(f"Price callback error: {e}")
    
    def _on_position_update(self, position_data: Dict[str, Any]):
        """Handle real-time position updates from WebSocket."""
        try:
            symbol = position_data.get('symbol')
            if not symbol:
                return
            
            # Check if position was closed (size = 0 or position removed)
            position_size = abs(float(position_data.get('size', 0)))
            
            if position_size == 0:
                # Position was closed
                if symbol in self.positions:
                    self.logger.info(f"Real-time position update: {symbol} position closed (size: {position_size})")
                    del self.positions[symbol]
                    self.save_positions_to_file()
            else:
                # Position was opened or modified
                if symbol not in self.positions:
                    # New position
                    self.logger.info(f"Real-time position update: {symbol} position opened (size: {position_size})")
                    position = Position(
                        symbol=symbol,
                        side=position_data.get('side', 'unknown'),
                        size=position_size,
                        entry_price=float(position_data.get('entry_price', 0)),
                        entry_time=datetime.now(),
                        strategy='unknown',
                        current_price=float(position_data.get('mark_price', 0))
                    )
                    self.positions[symbol] = position
                    self.save_positions_to_file()
                else:
                    # Existing position updated
                    local_position = self.positions[symbol]
                    old_size = local_position.size
                    if abs(position_size - old_size) > 0.001:  # Significant size change
                        self.logger.info(f"Real-time position update: {symbol} size changed {old_size} → {position_size}")
                        local_position.size = position_size
                        self.save_positions_to_file()
            
        except Exception as e:
            self.logger.error(f"Error handling position update: {e}")
    
    def _analyze_symbol(self, symbol: str):
        """Analyze a single symbol and execute strategies with per-strategy timeframes."""
        try:
            # Subscribe to real-time data for this symbol
            self._subscribe_to_symbol(symbol)
            
            # Check if we have sufficient data
            if not self.market_api.is_data_available(symbol):
                self.logger.debug(f"Insufficient data for {symbol}, skipping")
                return
            
            # Get current price (timeframe doesn't matter for current price)
            market_data = self.market_api.get_market_data(symbol)
            if not market_data:
                self.logger.warning(f"Could not get market data for {symbol}")
                return
            
            current_price = market_data['current_price']
            
            # Collect all signals from all strategies for this symbol
            collected_signals = []
            
            for strategy_name, strategy in self.strategies.items():
                # Handle special strategies that don't participate in conflict resolution
                if strategy_name == 'funding_rate_arbitrage':
                    # Funding rate arb is multi-leg and doesn't conflict with single-leg strategies
                    ohlcv = self.market_api.get_ohlcv(symbol, strategy.timeframe, self.ohlcv_limit)
                    if ohlcv is not None:
                        self._execute_strategy(symbol, strategy_name, strategy, ohlcv, current_price)
                    continue
                
                if strategy_name == 'momentum_factor':
                    # Momentum is handled separately in _run_portfolio_strategies
                    continue
                
                # Get OHLCV data for this strategy's timeframe
                ohlcv = self.market_api.get_ohlcv(symbol, strategy.timeframe, self.ohlcv_limit)
                if ohlcv is None or len(ohlcv) < 20:  # Need at least 20 candles for analysis
                    self.logger.debug(f"Insufficient {strategy.timeframe} OHLCV data for {symbol}/{strategy_name}")
                    continue
                
                # Generate signal without executing
                signal = self._generate_signal_for_strategy(symbol, strategy_name, strategy, ohlcv, current_price)
                if signal:
                    collected_signals.append({
                        'strategy_name': strategy_name,
                        'strategy': strategy,
                        'signal': signal,
                        'ohlcv': ohlcv,
                        'current_price': current_price
                    })
            
            # Resolve conflicts and execute the winning signal
            if collected_signals:
                winning_signal = self._resolve_signal_conflicts(symbol, collected_signals)
                if winning_signal:
                    self._execute_resolved_signal(symbol, winning_signal)
                
        except Exception as e:
            self.logger.error(f"Error analyzing {symbol}: {e}")
    
    def _generate_signal_for_strategy(self, symbol: str, strategy_name: str, strategy, ohlcv: pd.DataFrame, current_price: float) -> Optional[Dict[str, Any]]:
        """Generate a signal from a strategy without executing it."""
        try:
            # Gate new entries for selected strategies during change-point cooldown
            if self._is_strategy_entry_blocked(strategy_name):
                self.logger.info(
                    f"Entry gated for {strategy_name}/{symbol}: cooldown active ({self._entry_block_reason}), "
                    f"until {self._entry_block_until}"
                )
                return None

            # Check if strategy is enabled by the strategy selector
            if not self.strategy_selector.is_strategy_enabled(strategy_name):
                self.logger.debug(f"Strategy {strategy_name} is disabled by selector, skipping")
                return None
            
            # Skip strategies that handle their own signal generation differently
            if strategy_name in ['funding_rate_arbitrage', 'momentum_factor']:
                return None
            
            # Generate the signal based on strategy type
            if strategy_name == 'ou_mean_reversion':
                signal = strategy.generate_signal_for_symbol(symbol, ohlcv)
            else:
                signal = strategy.generate_signal(ohlcv)
            
            if signal:
                # Get strategy weight
                strategy_weight = self._get_effective_strategy_weight(strategy_name)
                signal['_strategy_weight'] = strategy_weight
                signal['_weighted_strength'] = signal.get('signal_strength', 0.5) * strategy_weight
                
            return signal
            
        except Exception as e:
            self.logger.error(f"Error generating signal for {strategy_name}/{symbol}: {e}")
            return None
    
    def _resolve_signal_conflicts(self, symbol: str, collected_signals: List[Dict]) -> Optional[Dict]:
        """
        Resolve conflicts when multiple strategies generate opposite signals for the same symbol.
        
        Uses the signal with the highest weighted strength.
        """
        if not collected_signals:
            return None
        
        if len(collected_signals) == 1:
            return collected_signals[0]
        
        # Group signals by direction (buy vs sell)
        buy_signals = [s for s in collected_signals if s['signal'].get('signal') == 'buy']
        sell_signals = [s for s in collected_signals if s['signal'].get('signal') == 'sell']
        
        # Check for conflict (both buy and sell signals exist)
        if buy_signals and sell_signals:
            # Get best signal from each direction
            best_buy = max(buy_signals, key=lambda s: s['signal'].get('_weighted_strength', 0))
            best_sell = max(sell_signals, key=lambda s: s['signal'].get('_weighted_strength', 0))
            
            buy_strength = best_buy['signal'].get('_weighted_strength', 0)
            sell_strength = best_sell['signal'].get('_weighted_strength', 0)
            
            self.logger.warning(
                f"⚠️ Signal conflict for {symbol}: "
                f"BUY ({best_buy['strategy_name']}, strength={buy_strength:.3f}) vs "
                f"SELL ({best_sell['strategy_name']}, strength={sell_strength:.3f})"
            )
            
            # Use the stronger signal
            if buy_strength > sell_strength:
                winner = best_buy
                loser_strategies = [s['strategy_name'] for s in sell_signals]
                self.logger.info(f"✓ Resolved: Using BUY from {winner['strategy_name']} (ignoring SELL from {loser_strategies})")
            elif sell_strength > buy_strength:
                winner = best_sell
                loser_strategies = [s['strategy_name'] for s in buy_signals]
                self.logger.info(f"✓ Resolved: Using SELL from {winner['strategy_name']} (ignoring BUY from {loser_strategies})")
            else:
                # Equal strength - skip the trade entirely
                self.logger.info(f"✗ Skipping {symbol}: Equal strength conflicting signals, no trade taken")
                return None
            
            return self._maybe_apply_strategy_exploration(symbol, collected_signals, winner)
        
        # No conflict - return the strongest signal
        best_signal = max(collected_signals, key=lambda s: s['signal'].get('_weighted_strength', 0))
        return self._maybe_apply_strategy_exploration(symbol, collected_signals, best_signal)

    def _maybe_apply_strategy_exploration(self, symbol: str, collected_signals: List[Dict], winner: Dict) -> Dict:
        """
        Optional exploration: if multiple strategies produce same-direction entry signals,
        occasionally execute an under-sampled strategy with reduced size to gather data.
        """
        try:
            cfg = (self.config.get("risk_management", {}) or {}).get("strategy_exploration", {}) or {}
            if not cfg.get("enabled", False):
                return winner

            # Only explore on entries (not when we already have a position)
            if symbol in self.positions:
                return winner

            direction = winner.get("signal", {}).get("signal")
            if direction not in ("buy", "sell"):
                return winner

            # Only explore among same-direction candidates to avoid flipping trade direction
            same_dir = [s for s in collected_signals if s.get("signal", {}).get("signal") == direction]
            if len(same_dir) <= 1:
                return winner

            min_trades = int(cfg.get("min_trades_per_strategy", 20))
            eps = float(cfg.get("epsilon", 0.15))
            close_delta = float(cfg.get("close_strength_delta", 0.10))
            size_scale = float(cfg.get("position_size_scale", 0.30))

            # Pull trade counts from selector rankings (updated periodically)
            def trade_count(strategy_name: str) -> int:
                r = getattr(self.strategy_selector, "strategy_rankings", {}).get(strategy_name)
                if not r:
                    return 0
                m = r.metrics or {}
                return int(m.get("total_trades", 0) or 0)

            under = [s for s in same_dir if trade_count(s["strategy_name"]) < min_trades]
            if not under:
                return winner

            # Determine if winner is only marginally better than runner-up
            sorted_same = sorted(same_dir, key=lambda s: s["signal"].get("_weighted_strength", 0), reverse=True)
            best_strength = float(sorted_same[0]["signal"].get("_weighted_strength", 0) or 0)
            second_strength = float(sorted_same[1]["signal"].get("_weighted_strength", 0) or 0)
            gap = 0.0
            denom = abs(best_strength) if abs(best_strength) > 1e-9 else 1.0
            gap = (best_strength - second_strength) / denom

            should_explore = (random.random() < eps) or (gap < close_delta)
            if not should_explore:
                return winner

            # Choose an under-sampled strategy with inverse-trade-count weighting
            weights = []
            for s in under:
                n = trade_count(s["strategy_name"])
                weights.append(1.0 / (1.0 + float(n)))

            chosen = random.choices(under, weights=weights, k=1)[0]
            chosen_sig = chosen.get("signal", {})
            chosen_sig["_exploration"] = True
            chosen_sig["_exploration_size_scale"] = size_scale
            chosen_sig["_exploration_reason"] = f"under_sampled(<{min_trades})"

            self.logger.info(
                f"🧪 Exploration override for {symbol}: choosing {chosen['strategy_name']} over "
                f"{winner.get('strategy_name')} (size_scale={size_scale:.2f})"
            )
            return chosen

        except Exception as e:
            self.logger.debug(f"Strategy exploration decision failed: {e}")
            return winner
    
    def _execute_resolved_signal(self, symbol: str, signal_data: Dict):
        """Execute a resolved signal after conflict resolution."""
        strategy_name = signal_data['strategy_name']
        strategy = signal_data['strategy']
        signal = signal_data['signal']
        ohlcv = signal_data['ohlcv']
        current_price = signal_data['current_price']
        
        self.logger.info(f"{strategy_name} signal for {symbol}: {signal['signal']} at {current_price}")
        
        # Check if this is a multi-leg signal
        if signal.get('signal_type') == 'multi_leg':
            self._handle_multi_leg_signal(symbol, signal, current_price, strategy_name, ohlcv)
            return
        
        # Get strategy weight from selector
        strategy_weight = self._get_effective_strategy_weight(strategy_name)
        
        # Check if we should act on the signal
        should_execute = self._should_execute_signal(symbol, signal, current_price, ohlcv, strategy_name)
        self.logger.info(f"Should execute {strategy_name} signal for {symbol}: {should_execute}")
        
        if should_execute:
            # Apply exploration size scaling (after sizing is computed)
            if signal.get("_exploration") and signal.get("_exploration_size_scale"):
                scale = float(signal.get("_exploration_size_scale") or 1.0)
                try:
                    if "size" in signal and signal["size"] is not None:
                        signal["size"] = float(signal["size"]) * scale
                    if "margin_required" in signal and signal["margin_required"] is not None:
                        signal["margin_required"] = float(signal["margin_required"]) * scale
                    # Also reduce effective signal_strength so any downstream logic is consistent
                    if "signal_strength" in signal and signal["signal_strength"] is not None:
                        signal["signal_strength"] = float(signal["signal_strength"]) * scale
                except Exception:
                    pass

            # Apply strategy weight to the signal strength for position sizing
            if 'signal_strength' in signal:
                signal['signal_strength'] *= strategy_weight
            
            self.logger.info(f"Executing {strategy_name} trade for {symbol} (weight: {strategy_weight:.2f})")
            self._execute_trade(symbol, signal, current_price, strategy_name, ohlcv)
        else:
            self.logger.info(f"Skipping {strategy_name} signal for {symbol} - conditions not met")
    
    def _run_portfolio_strategies(self, trading_pairs: List[str]):
        """
        Run cross-sectional/portfolio-level strategies.
        
        These strategies analyze ALL symbols at once rather than per-symbol.
        """
        try:
            # Momentum Factor Strategy
            if 'momentum_factor' in self.strategies:
                momentum_strategy = self.strategies['momentum_factor']
                
                if not self.strategy_selector.is_strategy_enabled('momentum_factor'):
                    return
                
                # Generate portfolio-level signals
                signals = momentum_strategy.generate_portfolio_signals(trading_pairs)
                
                if signals:
                    self.logger.info(f"Momentum strategy generated {len(signals)} rebalance signals")
                    
                    for signal in signals:
                        symbol = signal.get('symbol')
                        action = signal.get('signal')  # 'buy' or 'sell'
                        reason = signal.get('reason', '')
                        
                        self.logger.info(f"Momentum signal: {action.upper()} {symbol} - {reason}")
                        
                        # Execute the signal
                        # Get current price
                        market_data = self.market_api.get_market_data(symbol)
                        if not market_data:
                            continue
                        
                        current_price = market_data['current_price']
                        
                        # Get OHLCV for position sizing calculations
                        ohlcv = self.market_api.get_ohlcv(symbol, momentum_strategy.timeframe, self.ohlcv_limit)
                        if ohlcv is None:
                            continue
                        
                        # Check if we should execute
                        should_execute = self._should_execute_signal(
                            symbol, signal, current_price, ohlcv, 'momentum_factor'
                        )
                        
                        if should_execute:
                            # Apply regime-aware strategy weight to signal strength (if present) for sizing.
                            # Momentum signals may not include signal_strength; sizing will fallback to internal strength calc.
                            eff_w = self._get_effective_strategy_weight('momentum_factor')
                            if 'signal_strength' in signal:
                                signal['signal_strength'] *= eff_w
                            self._execute_trade(symbol, signal, current_price, 'momentum_factor', ohlcv)
                            
        except Exception as e:
            self.logger.error(f"Error running portfolio strategies: {e}")
    
    def _execute_strategy(self, symbol: str, strategy_name: str, strategy, ohlcv: pd.DataFrame, current_price: float):
        """Execute a single strategy."""
        try:
            # Gate new entries for selected strategies during change-point cooldown
            if self._is_strategy_entry_blocked(strategy_name):
                self.logger.info(
                    f"Entry gated for {strategy_name}/{symbol}: cooldown active ({self._entry_block_reason}), "
                    f"until {self._entry_block_until}"
                )
                return

            # Check if strategy is enabled by the strategy selector
            if not self.strategy_selector.is_strategy_enabled(strategy_name):
                self.logger.debug(f"Strategy {strategy_name} is disabled by selector, skipping")
                return
            
            # Generate signal based on strategy type
            signal = None
            
            if strategy_name == 'stat_arb':
                # Stat Arb needs special handling to fetch correlated pair data
                signal = strategy.generate_signal_with_symbol(symbol, ohlcv)
            elif strategy_name == 'funding_rate_arbitrage':
                # Funding Rate Arbitrage needs funding rate data and multi-leg position context
                signal = self._generate_funding_arb_signal(symbol, strategy)
            elif strategy_name == 'ou_mean_reversion':
                # OU Mean Reversion needs symbol context for parameter caching
                signal = strategy.generate_signal_for_symbol(symbol, ohlcv)
            elif strategy_name == 'momentum_factor':
                # Momentum is a cross-sectional strategy that ranks all symbols
                # It can't generate per-symbol signals - needs portfolio-level rebalancing
                # TODO: Implement periodic momentum rebalancing via generate_portfolio_signals
                return
            else:
                signal = strategy.generate_signal(ohlcv)
            
            if not signal:
                return
            
            self.logger.info(f"{strategy_name} signal for {symbol}: {signal['signal']} at {current_price}")
            
            # Check if this is a multi-leg signal
            if signal.get('signal_type') == 'multi_leg':
                self._handle_multi_leg_signal(symbol, signal, current_price, strategy_name, ohlcv)
                return
            
            # Standard single-leg signal handling
            # Get strategy weight from selector
            strategy_weight = self._get_effective_strategy_weight(strategy_name)
            self.logger.debug(f"Strategy {strategy_name} weight: {strategy_weight:.2f}")
            
            # Check if we should act on the signal
            should_execute = self._should_execute_signal(symbol, signal, current_price, ohlcv, strategy_name)
            self.logger.info(f"Should execute {strategy_name} signal for {symbol}: {should_execute}")
            
            if should_execute:
                # Apply strategy weight to the signal strength for position sizing
                if 'signal_strength' in signal:
                    signal['signal_strength'] *= strategy_weight
                
                self.logger.info(f"Executing {strategy_name} trade for {symbol} (weight: {strategy_weight:.2f})")
                self._execute_trade(symbol, signal, current_price, strategy_name, ohlcv)
            else:
                self.logger.info(f"Skipping {strategy_name} signal for {symbol} - conditions not met")
                
        except Exception as e:
            self.logger.error(f"Error executing {strategy_name} strategy for {symbol}: {e}")
            import traceback
            self.logger.error(f"Traceback: {traceback.format_exc()}")
    
    def _generate_funding_arb_signal(self, symbol: str, strategy) -> Optional[Dict[str, Any]]:
        """Generate signal for funding rate arbitrage strategy."""
        try:
            # Get funding rate from API
            funding_rate = self.market_api.get_funding_rate(symbol)
            if funding_rate is None:
                return None
            
            # Update funding cache
            strategy.update_funding_cache(symbol, funding_rate)
            funding_history = strategy.get_funding_history(symbol)
            
            # Check if we have an existing multi-leg position for this symbol
            existing_position = self._get_multi_leg_position_for_symbol(symbol)
            has_position = existing_position is not None
            position_entry_time = existing_position.entry_time if existing_position else None
            position_perp_side = None
            
            if existing_position:
                perp_leg = existing_position.get_leg('perp')
                if perp_leg:
                    position_perp_side = perp_leg.side
            
            # Generate signal
            return strategy.generate_signal_for_symbol(
                symbol=symbol,
                funding_rate=funding_rate,
                funding_history=funding_history,
                has_existing_position=has_position,
                position_entry_time=position_entry_time,
                position_perp_side=position_perp_side,
            )
            
        except Exception as e:
            self.logger.error(f"Error generating funding arb signal for {symbol}: {e}")
            return None
    
    def _get_multi_leg_position_for_symbol(self, symbol: str) -> Optional[MultiLegPosition]:
        """Find a multi-leg position that includes the given symbol."""
        for position in self.multi_leg_positions.values():
            if position.primary_symbol == symbol:
                return position
        return None
    
    def _handle_multi_leg_signal(self, symbol: str, signal: Dict[str, Any], current_price: float, 
                                strategy_name: str, ohlcv: pd.DataFrame):
        """Handle multi-leg trade signal."""
        self.execution_engine.handle_multi_leg_signal(
            symbol, signal, current_price, strategy_name, ohlcv, 
            self.strategies[strategy_name].calculate_signal_strength
        )
    
    def _get_leg_price(self, symbol: str, market_type: str) -> Optional[float]:
        """Delegate leg price fetching to execution engine."""
        return self.execution_engine.get_leg_price(symbol, market_type)

    def _check_liquidation_risks(self):
        """Delegate liquidation risk check to execution engine."""
        self.execution_engine.check_liquidation_risks(self.strategies)

    def _should_execute_signal(self, symbol: str, signal: Dict[str, Any], current_price: float, 
                              ohlcv: pd.DataFrame, strategy_name: str) -> bool:
        """Determine if we should execute a trading signal."""
        # Check if we already have a position
        if symbol in self.positions:
            position = self.positions[symbol]
            
            # If we have a position, only act on opposite signals
            if position.side == 'long' and signal['signal'] == 'buy':
                return False
            elif position.side == 'short' and signal['signal'] == 'sell':
                return False
        
        # Calculate signal strength and volatility
        signal_strength = self.strategies[strategy_name].calculate_signal_strength(ohlcv)
        market_volatility = self._calculate_market_volatility(ohlcv)
        
        # Check position limit before proceeding
        if not self._should_execute_with_position_limit(symbol, signal, signal_strength):
            return False
        
        # Calculate dynamic leveraged position size
        available_capital = self.portfolio_manager.calculate_available_capital_for_trading()
        # Keep a reserve for exploration trades by sizing normal trades using reduced available capital
        exploration_cfg = (self.config.get("risk_management", {}) or {}).get("strategy_exploration", {}) or {}
        reserve_pct = float(exploration_cfg.get("reserve_capital_pct", 0.10) or 0.0)
        is_exploration = bool(signal.get("_exploration"))
        reserved_amount = max(0.0, float(available_capital) * reserve_pct)
        available_capital_for_trade = float(available_capital)
        if (not is_exploration) and reserve_pct > 0:
            available_capital_for_trade = max(0.0, float(available_capital) - reserved_amount)

        self.logger.info(
            f"Calculating position size for {symbol}: available capital=${available_capital:.2f} "
            f"(trade_capital=${available_capital_for_trade:.2f}, reserve=${reserved_amount:.2f}), "
            f"signal_strength={signal_strength:.2f}, volatility={market_volatility:.2f}"
        )
        
        position_size, margin_required, leverage = self.leverage_manager.calculate_leveraged_position_size(
            symbol, current_price, available_capital_for_trade, strategy_name, signal_strength, market_volatility
        )
        
        self.logger.info(f"Position calculation for {symbol}: size={position_size:.4f}, margin=${margin_required:.2f}, leverage={leverage:.1f}x")
        
        # Check if we can open the position
        can_open = self.leverage_manager.can_open_position(symbol, margin_required, available_capital_for_trade)
        self.logger.info(f"Can open position for {symbol}: {can_open}")
        
        if not can_open:
            return False
        
        # Update signal with calculated size and leverage info
        signal['size'] = position_size
        signal['leverage'] = leverage
        signal['margin_required'] = margin_required
        signal['signal_strength'] = signal_strength
        signal['market_volatility'] = market_volatility
        
        return True
    
    def _execute_trade(self, symbol: str, signal: Dict[str, Any], current_price: float, strategy_name: str, ohlcv: pd.DataFrame):
        """Execute a trade based on signal."""
        # Delegate to execution engine
        self.execution_engine.execute_trade(symbol, signal, current_price, strategy_name, ohlcv, self.strategies)
    
    def close_position(self, symbol: str, reason: str = "manual") -> bool:
        """Close a position and record it."""
        return self.execution_engine.close_position(symbol, reason)

    def get_performance_summary(self) -> Dict[str, Any]:
        """Get comprehensive performance summary from performance tracker."""
        # Get comprehensive metrics from performance tracker
        tracker_summary = self.performance_tracker.get_performance_summary()
        overall_metrics = tracker_summary['overall']
        
        # Get pair performance from selector
        pair_performance = self.pair_selector.get_pair_performance_summary()
        
        # Get margin and risk summaries
        margin_summary = self.leverage_manager.get_margin_summary()
        risk_summary = self.leverage_manager.get_risk_summary()
        
        return {
            # Basic metrics
            'total_trades': overall_metrics['total_trades'],
            'winning_trades': overall_metrics['winning_trades'],
            'losing_trades': overall_metrics['losing_trades'],
            'win_rate': overall_metrics['win_rate'],
            'total_pnl': overall_metrics['total_pnl'],
            'open_positions': len(self.positions),
            
            # PnL metrics
            'average_pnl': overall_metrics['average_pnl'],
            'average_win': overall_metrics['average_win'],
            'average_loss': overall_metrics['average_loss'],
            'largest_win': overall_metrics['largest_win'],
            'largest_loss': overall_metrics['largest_loss'],
            'gross_profit': overall_metrics['gross_profit'],
            'gross_loss': overall_metrics['gross_loss'],
            
            # Risk metrics
            'profit_factor': overall_metrics['profit_factor'],
            'risk_reward_ratio': overall_metrics['risk_reward_ratio'],
            'expectancy': overall_metrics['expectancy'],
            'max_drawdown': overall_metrics['max_drawdown'],
            'max_drawdown_percentage': overall_metrics['max_drawdown_percentage'],
            
            # Advanced metrics
            'sharpe_ratio': overall_metrics['sharpe_ratio'],
            'sortino_ratio': overall_metrics['sortino_ratio'],
            'calmar_ratio': overall_metrics['calmar_ratio'],
            
            # Streak metrics
            'max_win_streak': overall_metrics['max_win_streak'],
            'max_lose_streak': overall_metrics['max_lose_streak'],
            'current_win_streak': overall_metrics['current_win_streak'],
            'current_lose_streak': overall_metrics['current_lose_streak'],
            
            # Time metrics
            'average_trade_duration_hours': overall_metrics.get('average_trade_duration_hours', 0),
            'exit_reasons': overall_metrics.get('exit_reasons', {}),
            
            # Strategy breakdown
            'strategy_breakdown': tracker_summary['strategy_breakdown'],
            
            # Recent performance
            'recent_7_days': tracker_summary['recent_7_days'],
            'daily_pnl': tracker_summary['daily_pnl'],
            'monthly_pnl': tracker_summary['monthly_pnl'],
            
            # Symbol analysis
            'top_symbols': tracker_summary['top_symbols'],
            'worst_symbols': tracker_summary['worst_symbols'],
            
            # Existing summaries
            'pair_performance': pair_performance,
            'data_summary': self.market_api.get_data_summary(),
            'margin_summary': margin_summary,
            'risk_summary': risk_summary,
            'dynamic_leverage': {
                'enabled': True,
                'strategy_ranges': {
                    'moving_average': '8-15x',
                    'rsi': '12-20x',
                    'scalping': '15-25x',
                },
            },
        }
    
    def log_performance_report(self):
        """Log a comprehensive performance report."""
        self.performance_tracker.log_performance_report()
        # Also log strategy rankings
        self.strategy_selector._log_rankings()
    
    def get_strategy_performance(self, strategy: str) -> Dict[str, Any]:
        """Get performance metrics for a specific strategy."""
        metrics = self.performance_tracker.get_strategy_metrics(strategy)
        return metrics.to_dict()
    
    def get_all_strategy_performance(self) -> Dict[str, Dict[str, Any]]:
        """Get performance metrics for all strategies."""
        all_metrics = self.performance_tracker.get_all_strategy_metrics()
        return {name: metrics.to_dict() for name, metrics in all_metrics.items()}
    
    def get_strategy_rankings(self) -> Dict[str, Any]:
        """Get current strategy rankings summary."""
        return self.strategy_selector.get_rankings_summary()
    
    def get_enabled_strategies(self) -> List[str]:
        """Get list of currently enabled strategies."""
        return self.strategy_selector.get_enabled_strategies()
    
    def force_pair_rescan(self):
        """Force a rescan of trading pairs."""
        self.pair_selector.force_rescan()
        self.logger.info("Forced pair rescan")
    
    def set_max_pairs_to_trade(self, max_pairs: int):
        """
        Set the maximum number of trading pairs.
        
        Args:
            max_pairs: Maximum number of pairs to trade
        """
        if max_pairs <= 0:
            self.logger.error("Max pairs must be greater than 0")
            return
        
        self.max_pairs_to_trade = max_pairs
        self.logger.info(f"Updated max pairs to trade: {max_pairs}")
        
        # Force a rescan to apply the new limit
        self.force_pair_rescan()
    
    def get_max_pairs_to_trade(self) -> int:
        """
        Get the current maximum number of trading pairs.
        
        Returns:
            Current max pairs limit
        """
        return self.max_pairs_to_trade
    
    def get_current_pair_count(self) -> int:
        """
        Get the current number of trading pairs.
        
        Returns:
            Current number of pairs being traded
        """
        return len(self.pair_selector.get_current_pairs())
    
    def _check_position_limit(self, max_allocation_pct: Optional[float] = None) -> bool:
        """
        Check if we've reached the position limit based on capital at risk.
        
        Returns:
            True if position limit reached, False otherwise
        """
        if max_allocation_pct is None:
            max_allocation_pct = self.max_positions_percentage

        # Check portfolio allocation to see if we're at the limit
        allocation = self._check_portfolio_allocation()
        current_allocation = allocation['allocation_percentage']
        
        # Log the current allocation status
        self.logger.info(
            f"Position limit check: {len(self.positions)} positions, {current_allocation:.1f}% of portfolio at risk "
            f"(max: {float(max_allocation_pct):.1f}%)"
        )
        
        # Return True if we're at or exceeding the allocation limit
        return current_allocation >= float(max_allocation_pct)
    
    def _get_position_profitability_score(self, symbol: str, new_signal_strength: float) -> float:
        """
        Calculate a comprehensive profitability score for a position.
        
        Score components:
        - Current PnL (25%): Unrealized profit/loss percentage
        - Expected Value (25%): Distance to TP vs SL, risk/reward
        - Strategy Performance (25%): Historical win rate and Sharpe from database
        - Time & Momentum (25%): Position age and price momentum
        
        Args:
            symbol: Symbol of the position
            new_signal_strength: Signal strength of the new potential trade
            
        Returns:
            Profitability score (higher = more profitable to keep)
        """
        if symbol not in self.positions:
            return 0.0
        
        position = self.positions[symbol]
        current_price = self.market_api.get_current_price(symbol)
        
        if not current_price:
            return 0.0
        
        # ===== 1. Current PnL Score (25%) =====
        pnl_percentage = position.unrealized_pnl_percentage or 0.0
        # Normalize PnL to 0-1 scale (cap at +/- 10%)
        pnl_score = max(-1.0, min(1.0, pnl_percentage / 10.0))
        # Shift to 0-1 range
        pnl_score = (pnl_score + 1.0) / 2.0
        
        # ===== 2. Expected Value Score (25%) =====
        ev_score = 0.5  # Default neutral
        if position.take_profit and position.stop_loss:
            # Calculate distance to TP and SL
            if position.side == 'long':
                tp_distance = (position.take_profit - current_price) / current_price
                sl_distance = (current_price - position.stop_loss) / current_price
            else:
                tp_distance = (current_price - position.take_profit) / current_price
                sl_distance = (position.stop_loss - current_price) / current_price
            
            # Risk/reward ratio from current price
            if sl_distance > 0:
                rr_ratio = tp_distance / sl_distance
                # Score based on R:R - higher is better
                ev_score = min(1.0, rr_ratio / 3.0)  # Cap at 3:1 R:R
            
            # Bonus if already past breakeven and heading to TP
            if pnl_percentage > 0 and tp_distance > 0:
                ev_score = min(1.0, ev_score + 0.2)
        
        # ===== 3. Strategy Performance Score (25%) =====
        strategy_score = 0.5  # Default neutral
        strategy_name = position.strategy
        
        if strategy_name and self.performance_tracker:
            try:
                # Get strategy stats from database
                strategy_stats = self.performance_tracker.db.get_strategy_stats(strategy_name)
                
                # Win rate component (0-1)
                total_trades = strategy_stats.get('total_trades', 0) or 0
                winning_trades = strategy_stats.get('winning_trades', 0) or 0
                if total_trades >= 5:  # Need minimum trades for reliability
                    win_rate = winning_trades / total_trades
                    win_rate_score = win_rate  # Already 0-1
                else:
                    win_rate_score = 0.5  # Neutral if insufficient data
                
                # Profit factor component
                gross_profit = strategy_stats.get('gross_profit', 0) or 0
                gross_loss = strategy_stats.get('gross_loss', 0) or 1  # Avoid div by 0
                profit_factor = gross_profit / max(gross_loss, 0.01)
                pf_score = min(1.0, profit_factor / 2.0)  # Cap at PF of 2.0
                
                # Strategy weight from selector (if available)
                weight_score = 0.5
                if self.strategy_selector and strategy_name in self.strategy_selector.strategy_rankings:
                    ranking = self.strategy_selector.strategy_rankings[strategy_name]
                    weight_score = min(1.0, ranking.weight)  # Weight is typically 0-1
                
                # Combined strategy score
                strategy_score = (win_rate_score * 0.4) + (pf_score * 0.3) + (weight_score * 0.3)
                
            except Exception as e:
                self.logger.debug(f"Could not get strategy stats for {strategy_name}: {e}")
        
        # ===== 4. Time & Momentum Score (25%) =====
        # Time factor - newer positions get slight preference (they haven't had chance to prove themselves)
        time_open = (datetime.now() - position.entry_time).total_seconds() / 3600  # hours
        
        # Sweet spot is 1-6 hours (had time to develop but not stale)
        if time_open < 1:
            time_score = 0.6  # Very new, slight penalty
        elif time_open < 6:
            time_score = 0.8  # Optimal range
        elif time_open < 12:
            time_score = 0.6  # Getting older
        elif time_open < 24:
            time_score = 0.4  # Stale
        else:
            time_score = 0.2  # Very stale
        
        # Momentum bonus - is the position moving in our favor?
        momentum_score = 0.5
        if position.highest_price and position.lowest_price and position.entry_price:
            if position.side == 'long':
                # For longs, check if we've made progress toward TP
                progress = (current_price - position.entry_price) / position.entry_price
                if progress > 0:
                    momentum_score = min(1.0, 0.5 + progress * 5)  # Bonus for positive progress
                else:
                    momentum_score = max(0.0, 0.5 + progress * 5)  # Penalty for negative progress
            else:
                # For shorts, inverse
                progress = (position.entry_price - current_price) / position.entry_price
                if progress > 0:
                    momentum_score = min(1.0, 0.5 + progress * 5)
                else:
                    momentum_score = max(0.0, 0.5 + progress * 5)
        
        time_momentum_score = (time_score * 0.5) + (momentum_score * 0.5)
        
        # ===== Final Score Calculation =====
        final_score = (
            (pnl_score * 0.25) +
            (ev_score * 0.25) +
            (strategy_score * 0.25) +
            (time_momentum_score * 0.25)
        )
        
        self.logger.debug(
            f"Position score for {symbol}: PnL={pnl_score:.2f}, EV={ev_score:.2f}, "
            f"Strategy={strategy_score:.2f}, TimeMom={time_momentum_score:.2f}, Final={final_score:.2f}"
        )
        
        return final_score
    
    def _close_least_profitable_position(self, new_signal_strength: float) -> bool:
        """
        Close the least profitable position to make room for a new trade.
        
        Uses comprehensive scoring that considers:
        - Current PnL
        - Expected value (distance to TP/SL)
        - Strategy historical performance
        - Position age and momentum
        
        Args:
            new_signal_strength: Signal strength of the new potential trade (0-1 scale)
            
        Returns:
            True if a position was closed, False otherwise
        """
        if not self.positions:
            return False
        
        # Calculate profitability scores for all positions
        position_scores = {}
        for symbol in self.positions:
            score = self._get_position_profitability_score(symbol, new_signal_strength)
            position_scores[symbol] = score
        
        # Log all position scores for transparency
        self.logger.info("=== Position Ranking for Capital Rotation ===")
        sorted_positions = sorted(position_scores.items(), key=lambda x: x[1], reverse=True)
        for rank, (sym, score) in enumerate(sorted_positions, 1):
            pos = self.positions.get(sym)
            pnl = pos.unrealized_pnl if pos else 0
            strategy = pos.strategy if pos else 'unknown'
            self.logger.info(f"  #{rank} {sym} ({strategy}): score={score:.3f}, PnL=${pnl:.2f}")
        self.logger.info(f"  New signal strength: {new_signal_strength:.3f}")
        self.logger.info("=" * 45)
        
        # Find the position with the lowest score
        least_profitable_symbol = min(position_scores.keys(), key=lambda x: position_scores[x])
        least_profitable_score = position_scores[least_profitable_symbol]
        least_profitable_pos = self.positions.get(least_profitable_symbol)
        
        # Scores are now 0-1 normalized, so use absolute thresholds
        # At position limit: new signal must be at least 0.1 points higher (10% of scale)
        # Below limit: new signal must be at least 0.2 points higher (20% of scale)
        
        at_limit = self._check_position_limit()
        threshold = 0.1 if at_limit else 0.2
        
        score_difference = new_signal_strength - least_profitable_score
        
        if score_difference > threshold:
            reason = "position_limit" if at_limit else "capital_rotation"
            self.logger.info(
                f"{'⚡ CAPITAL ROTATION' if at_limit else '🔄 CAPITAL ROTATION'}: "
                f"Closing {least_profitable_symbol} ({least_profitable_pos.strategy if least_profitable_pos else 'unknown'}) "
                f"[score={least_profitable_score:.3f}, PnL=${least_profitable_pos.unrealized_pnl:.2f if least_profitable_pos else 0}] "
                f"for new trade [strength={new_signal_strength:.3f}] "
                f"(diff={score_difference:.3f} > threshold={threshold:.2f})"
            )
            self.close_position(least_profitable_symbol, reason)
            return True
        else:
            self.logger.info(
                f"❌ Capital rotation rejected: {least_profitable_symbol} score ({least_profitable_score:.3f}) "
                f"+ threshold ({threshold:.2f}) > new signal ({new_signal_strength:.3f})"
            )
        
        return False
    
    def _check_portfolio_allocation(self) -> Dict[str, Any]:
        """
        Check the current portfolio allocation to ensure we're not exceeding limits.
        
        Returns:
            Dictionary with allocation information
        """
        total_capital_at_risk = 0.0
        position_details = {}
        
        for symbol, position in self.positions.items():
            # Use the capital_at_risk from the position if available, otherwise calculate it
            if position.capital_at_risk is not None:
                capital_at_risk = position.capital_at_risk
            else:
                # Fallback calculation
                position_value = position.size * position.entry_price
                capital_at_risk = position_value
            
            total_capital_at_risk += capital_at_risk
            available_capital = self.portfolio_manager.calculate_available_capital_for_trading()
            
            # Handle division by zero
            if available_capital <= 0:
                percentage = 0.0
            else:
                percentage = (capital_at_risk / available_capital) * 100
            
            position_details[symbol] = {
                'value': capital_at_risk,
                'percentage': percentage,
                'notional_value': position.size * position.entry_price
            }
        
        available_capital = self.portfolio_manager.calculate_available_capital_for_trading()
        
        # Handle division by zero
        if available_capital <= 0:
            allocation_percentage = 0.0
        else:
            allocation_percentage = (total_capital_at_risk / available_capital) * 100
        
        return {
            'total_capital_at_risk': total_capital_at_risk,
            'available_capital': self.portfolio_manager.calculate_available_capital_for_trading(),
            'allocation_percentage': allocation_percentage,
            'position_details': position_details,
            'max_allocation': self.max_positions_percentage
        }

    def _should_execute_with_position_limit(self, symbol: str, signal: Dict[str, Any], signal_strength: float) -> bool:
        """
        Check if we should execute a trade considering position limits.
        
        Args:
            symbol: Symbol to trade
            signal: Trading signal
            signal_strength: Signal strength
            
        Returns:
            True if trade should be executed, False otherwise
        """
        # Check if we already have a position for this symbol
        if symbol in self.positions:
            self.logger.info(f"Already have a position for {symbol}, skipping trade")
            return False
        
        # Check portfolio allocation first
        allocation = self._check_portfolio_allocation()
        exploration_cfg = (self.config.get("risk_management", {}) or {}).get("strategy_exploration", {}) or {}
        reserve_pct = float(exploration_cfg.get("reserve_capital_pct", 0.10) or 0.0)
        is_exploration = bool(signal.get("_exploration"))
        effective_max = float(self.max_positions_percentage)
        if (not is_exploration) and reserve_pct > 0:
            # Keep reserve_pct of available capital "free" by lowering the max allocation threshold for normal trades.
            effective_max = max(0.0, float(self.max_positions_percentage) - (reserve_pct * 100.0))

        self.logger.info(
            f"Portfolio allocation for {symbol}: {allocation['allocation_percentage']:.1f}% "
            f"(max: {effective_max:.1f}%, exploration={is_exploration}, reserve_pct={reserve_pct:.2f})"
        )
        
        if allocation['allocation_percentage'] >= effective_max:
            self.logger.warning(
                f"Portfolio allocation limit reached: {allocation['allocation_percentage']:.1f}% >= {effective_max:.1f}%"
            )
            # Try to close least profitable position to make room
            if self._close_least_profitable_position(signal_strength):
                self.logger.info(f"Closed least profitable position to make room for {symbol}")
                return True
            return False
        
        # If we haven't reached the position count limit, allow the trade
        position_limit_reached = self._check_position_limit(max_allocation_pct=effective_max)
        self.logger.info(f"Position limit check for {symbol}: {len(self.positions)} positions, limit reached: {position_limit_reached}")
        
        if not position_limit_reached:
            self.logger.info(f"Position limit not reached for {symbol}, allowing trade")
            return True
        
        # If we have reached the position count limit, try to close a less profitable position
        if self._close_least_profitable_position(signal_strength):
            self.logger.info(f"Closed least profitable position to make room for {symbol}")
            return True
        
        # If we couldn't close any positions, don't execute the trade
        self.logger.warning(f"Position limit reached ({len(self.positions)} positions), skipping trade for {symbol}")
        return False 

    def save_positions_to_file(self):
        """Save current positions to a JSON file."""
        return self.execution_engine.save_positions_to_file()
    
    def load_positions_from_file(self):
        """Load positions from JSON file."""
        return self.execution_engine.load_positions_from_file()
    
    def update_position_prices(self):
        """Update current prices for all open positions (single-leg and multi-leg)."""
        # Update single-leg positions
        for symbol, position in self.positions.items():
            current_price = self.market_api.get_current_price(symbol)
            if current_price:
                position.current_price = current_price
        
        # Update multi-leg positions
        for position_id, position in self.multi_leg_positions.items():
            for leg in position.legs:
                leg_price = self._get_leg_price(leg.symbol, leg.market_type)
                if leg_price:
                    leg.current_price = leg_price
    
    def get_positions_summary(self) -> Dict[str, Any]:
        """Get a summary of all open positions with PnL."""
        self.update_position_prices()
        
        positions_summary = []
        total_unrealized_pnl = 0.0
        total_capital_at_risk = 0.0
        
        for symbol, position in self.positions.items():
            if position.current_price is None:
                continue
            
            unrealized_pnl = position.unrealized_pnl
            unrealized_pnl_percentage = position.unrealized_pnl_percentage
            capital_at_risk_pnl_percentage = position.capital_at_risk_pnl_percentage
            position_value = position.current_price * position.size
            capital_at_risk = position.capital_at_risk or position_value  # Use capital_at_risk if available
            
            if unrealized_pnl is not None:
                total_unrealized_pnl += unrealized_pnl
                total_capital_at_risk += capital_at_risk
            
            positions_summary.append({
                'symbol': symbol,
                'side': position.side,
                'size': position.size,
                'entry_price': position.entry_price,
                'current_price': position.current_price,
                'unrealized_pnl': unrealized_pnl,
                'unrealized_pnl_percentage': unrealized_pnl_percentage,
                'capital_at_risk_pnl_percentage': capital_at_risk_pnl_percentage,
                'position_value': position_value,
                'capital_at_risk': capital_at_risk,
                'strategy': position.strategy,
                'time_open': (datetime.now() - position.entry_time).total_seconds() / 3600,  # hours
            })
        
        # Add multi-leg positions
        multi_leg_summary = []
        for position_id, position in self.multi_leg_positions.items():
            unrealized_pnl = position.unrealized_pnl
            capital_at_risk = position.capital_at_risk or position.total_notional
            
            if unrealized_pnl is not None:
                total_unrealized_pnl += unrealized_pnl
                total_capital_at_risk += capital_at_risk
            
            multi_leg_summary.append({
                'position_id': position_id,
                'strategy': position.strategy,
                'symbol': position.primary_symbol,
                'legs': [{
                    'symbol': leg.symbol,
                    'market_type': leg.market_type,
                    'side': leg.side,
                    'size': leg.size,
                    'entry_price': leg.entry_price,
                    'current_price': leg.current_price,
                } for leg in position.legs],
                'unrealized_pnl': unrealized_pnl,
                'unrealized_pnl_percentage': position.unrealized_pnl_percentage,
                'net_delta': position.net_delta,
                'total_notional': position.total_notional,
                'capital_at_risk': capital_at_risk,
                'time_open': (datetime.now() - position.entry_time).total_seconds() / 3600,
            })
        
        return {
            'positions': positions_summary,
            'multi_leg_positions': multi_leg_summary,
            'total_positions': len(positions_summary) + len(multi_leg_summary),
            'total_unrealized_pnl': total_unrealized_pnl,
            'total_capital_at_risk': total_capital_at_risk,
            'average_pnl_percentage': (total_unrealized_pnl / total_capital_at_risk * 100) if total_capital_at_risk > 0 else 0,
        }
    
    def display_positions_pnl(self):
        """Display current positions with PnL information."""
        if not self.positions and not self.multi_leg_positions:
            return
        
        # Get portfolio allocation info
        allocation = self._check_portfolio_allocation()
        
        self.logger.info("=" * 60)
        self.logger.info("📊 OPEN POSITIONS SUMMARY")
        self.logger.info("=" * 60)
        
        # Display allocation info
        self.logger.info(f"Portfolio Allocation: {allocation['allocation_percentage']:.1f}% / {allocation['max_allocation']}%")
        self.logger.info(f"Total Capital at Risk: ${allocation['total_capital_at_risk']:.2f}")
        self.logger.info(f"Available Capital: ${allocation['available_capital']:.2f}")
        
        positions_summary = self.get_positions_summary()
        
        for position_info in positions_summary['positions']:
            symbol = position_info['symbol']
            side = position_info['side']
            size = position_info['size']
            entry_price = position_info['entry_price']
            current_price = position_info['current_price']
            pnl = position_info['unrealized_pnl']
            pnl_percentage = position_info['unrealized_pnl_percentage']
            capital_at_risk_pnl_percentage = position_info['capital_at_risk_pnl_percentage']
            time_open = position_info['time_open']
            
            # Get allocation percentage for this position
            pos_allocation = allocation['position_details'].get(symbol, {}).get('percentage', 0)
            notional_value = allocation['position_details'].get(symbol, {}).get('notional_value', 0)
            capital_at_risk = position_info['capital_at_risk']
            
            # Determine status emoji
            if pnl > 0:
                status = "🟢 PROFIT"
            elif pnl < 0:
                status = "🔴 LOSS"
            else:
                status = "⚪ BREAKEVEN"
            
            self.logger.info(f"{symbol:<12} {side:<5} {size:>8.3f} @ ${entry_price:<8.4f} → ${current_price:<8.4f} | PnL: ${pnl:>8.2f} ({pnl_percentage:>6.2f}% / {capital_at_risk_pnl_percentage:>6.2f}%) | Time: {time_open:>4.1f}h | {status} | Risk: {pos_allocation:>5.1f}% | Capital: ${capital_at_risk:>8.2f}")
        
        # Display multi-leg positions (funding arb, stat arb, etc.)
        if positions_summary.get('multi_leg_positions'):
            self.logger.info("-" * 60)
            self.logger.info("📊 MULTI-LEG POSITIONS (Delta-Neutral)")
            self.logger.info("-" * 60)
            
            for ml_pos in positions_summary['multi_leg_positions']:
                pnl = ml_pos.get('unrealized_pnl', 0) or 0
                pnl_pct = ml_pos.get('unrealized_pnl_percentage', 0) or 0
                net_delta = ml_pos.get('net_delta', 0) or 0
                time_open = ml_pos.get('time_open', 0)
                capital = ml_pos.get('capital_at_risk', 0)
                
                # Delta-neutral indicator
                delta_status = "✓ NEUTRAL" if abs(net_delta) < 0.01 else f"⚠ Δ={net_delta:.4f}"
                
                if pnl > 0:
                    status = "🟢"
                elif pnl < 0:
                    status = "🔴"
                else:
                    status = "⚪"
                
                self.logger.info(f"🔗 {ml_pos['strategy']:<20} | {ml_pos['symbol']:<8} | PnL: ${pnl:>8.2f} ({pnl_pct:>6.2f}%) | {delta_status} | Time: {time_open:>4.1f}h {status}")
                
                # Show individual legs
                for leg in ml_pos.get('legs', []):
                    leg_pnl = 0
                    if leg.get('current_price') and leg.get('entry_price'):
                        if leg['side'] == 'long':
                            leg_pnl = (leg['current_price'] - leg['entry_price']) * leg['size']
                        else:
                            leg_pnl = (leg['entry_price'] - leg['current_price']) * leg['size']
                    
                    leg_status = "🟢" if leg_pnl > 0 else ("🔴" if leg_pnl < 0 else "⚪")
                    self.logger.info(f"   └─ {leg['market_type']:<5} {leg['side']:<5} {leg['size']:>10.6f} {leg['symbol']:<12} @ ${leg.get('entry_price', 0):.4f} → ${leg.get('current_price', 0):.4f} | ${leg_pnl:>8.2f} {leg_status}")
        
        self.logger.info("-" * 60)
        total_positions = len(positions_summary.get('positions', [])) + len(positions_summary.get('multi_leg_positions', []))
        self.logger.info(f"TOTAL: {total_positions} positions | PnL: ${positions_summary['total_unrealized_pnl']:>8.2f} | Capital at Risk: ${allocation['total_capital_at_risk']:>8.2f}")
        self.logger.info("=" * 60) 

    def _cleanup_open_orders(self):
        """Clean up any existing open orders."""
        try:
            open_orders = self.market_api.get_open_orders()
            
            if open_orders:
                self.logger.info(f"Found {len(open_orders)} open orders, cancelling them...")
                
                for order in open_orders:
                    order_id = order.get('order_id')
                    symbol = order.get('symbol')
                    side = order.get('side')
                    size = order.get('size')
                    price = order.get('price')
                    
                    self.logger.info(f"Cancelling open order: {symbol} {side} {size} @ {price}")
                    
                    if self.market_api.cancel_order(symbol, order_id):
                        self.logger.info(f"Successfully cancelled order {order_id}")
                    else:
                        self.logger.warning(f"Failed to cancel order {order_id}")
                
                # Wait a moment for cancellations to process
                time.sleep(2)
            else:
                self.logger.info("No open orders found")
                
        except Exception as e:
            self.logger.error(f"Error cleaning up open orders: {e}") 

    def _monitor_and_close_positions(self, emergency_threshold: float,
                                   total_closed: int, emergency_stops: int, last_emergency_check: int):
        """Monitor positions and close them if they meet closure criteria."""
        try:
            current_time = time.time()
            
            self.logger.debug(f"Position monitoring: checking {len(self.positions)} positions")
            
            # Check for positions that need to be closed
            positions_to_close = []
            
            for symbol, position in self.positions.items():
                if not position.current_price:
                    continue
                
                close_reason = self._should_close_position(position)
                if close_reason:
                    positions_to_close.append((symbol, close_reason))
            
            # Close positions
            for symbol, reason in positions_to_close:
                if self.close_position(symbol, reason):
                    total_closed += 1
            
            # Emergency stop check (every 30 seconds)
            if current_time - last_emergency_check >= 30:
                if self._check_emergency_stop(emergency_threshold):
                    emergency_stops += 1
                    self.logger.error("🚨 EMERGENCY STOP TRIGGERED - Closing all positions!")
                    self.close_all_positions("emergency_stop")
                    return
                last_emergency_check = current_time
            
            # Log monitoring summary if positions were closed
            if positions_to_close:
                self.logger.info(f"Position monitoring: closed {len(positions_to_close)} positions")
                for symbol, reason in positions_to_close:
                    self.logger.info(f"  - {symbol}: {reason}")
            else:
                # Log that monitoring is active but no positions need closing
                self.logger.debug(f"Position monitoring active: {len(self.positions)} positions checked, none need closing")
                    
        except Exception as e:
            self.logger.error(f"Error in position monitoring: {e}")
    
    def _should_close_position(self, position: Position) -> Optional[str]:
        """
        Determine if a position should be closed.
        
        Exit priority:
        1. Strategy-specific exit conditions (should_exit method)
        2. Global fallback stop loss (protects against 3%+ account loss)
        3. Strategy-specific take profit
        
        Note: Position timeout removed - if strategy is still valid, position stays open.
        
        Args:
            position: Position to check

        Returns:
            Reason for closure if should close, None otherwise
        """
        if not position.current_price:
            return None
        
        # 1. Check strategy-specific exit conditions first
        strategy_name = position.strategy
        if strategy_name and strategy_name in self.strategies:
            strategy = self.strategies[strategy_name]
            
            # Build current_data for strategy exit check
            current_data = {}
            try:
                # Get OHLCV data for this symbol
                ohlcv = self.market_api.get_ohlcv(position.symbol, strategy.timeframe, strategy.ohlcv_limit)
                if ohlcv is not None:
                    current_data['ohlcv'] = ohlcv
            except Exception as e:
                self.logger.debug(f"Could not get OHLCV for exit check: {e}")
            
            # Call strategy's should_exit method
            should_exit, exit_reason = strategy.should_exit(
                position, position.current_price, current_data
            )
            
            if should_exit and exit_reason:
                return f"strategy_exit:{exit_reason}"
        
        # 2. Update trailing stop if enabled
        if position.trailing_stop_enabled:
            current_price = position.current_price
            
            if position.side == 'long':
                # Update highest price watermark
                if position.highest_price is None or current_price > position.highest_price:
                    position.highest_price = current_price
                
                # Check if trailing stop should be activated
                gain_pct = (current_price - position.entry_price) / position.entry_price
                
                if not position.trailing_stop_active and gain_pct >= position.trailing_stop_activation_pct:
                    position.trailing_stop_active = True
                    self.logger.info(f"🎯 Trailing stop ACTIVATED for {position.symbol} at {gain_pct*100:.2f}% gain")
                
                # Update trailing stop loss if active
                if position.trailing_stop_active and position.highest_price:
                    new_trailing_sl = position.highest_price * (1 - position.trailing_stop_pct)
                    
                    # Only move stop loss UP (more protective), never down
                    if position.stop_loss is None or new_trailing_sl > position.stop_loss:
                        old_sl = position.stop_loss
                        position.stop_loss = new_trailing_sl
                        self.logger.debug(f"Trailing SL updated for {position.symbol}: "
                                         f"{old_sl:.4f} → {new_trailing_sl:.4f} (high: {position.highest_price:.4f})")
            
            else:  # short position
                # Update lowest price watermark
                if position.lowest_price is None or current_price < position.lowest_price:
                    position.lowest_price = current_price
                
                # Check if trailing stop should be activated
                gain_pct = (position.entry_price - current_price) / position.entry_price
                
                if not position.trailing_stop_active and gain_pct >= position.trailing_stop_activation_pct:
                    position.trailing_stop_active = True
                    self.logger.info(f"🎯 Trailing stop ACTIVATED for {position.symbol} (short) at {gain_pct*100:.2f}% gain")
                
                # Update trailing stop loss if active
                if position.trailing_stop_active and position.lowest_price:
                    new_trailing_sl = position.lowest_price * (1 + position.trailing_stop_pct)
                    
                    # Only move stop loss DOWN (more protective), never up
                    if position.stop_loss is None or new_trailing_sl < position.stop_loss:
                        old_sl = position.stop_loss
                        position.stop_loss = new_trailing_sl
                        self.logger.debug(f"Trailing SL updated for {position.symbol} (short): "
                                         f"{old_sl:.4f} → {new_trailing_sl:.4f} (low: {position.lowest_price:.4f})")
        
        # 3. Global fallback stop loss (safety net) - includes trailing stop
        if position.stop_loss:
            if position.side == 'long' and position.current_price <= position.stop_loss:
                reason = "trailing_stop" if position.trailing_stop_active else "stop_loss"
                return reason
            elif position.side == 'short' and position.current_price >= position.stop_loss:
                reason = "trailing_stop" if position.trailing_stop_active else "stop_loss"
                return reason
        
        # 4. Take profit check
        if position.take_profit:
            if position.side == 'long' and position.current_price >= position.take_profit:
                return "take_profit"
            elif position.side == 'short' and position.current_price <= position.take_profit:
                return "take_profit"
        
        return None
    
    def _check_emergency_stop(self, threshold: float) -> bool:
        """Check if emergency stop should be triggered."""
        try:
            total_loss = 0.0
            total_capital_at_risk = 0.0
            
            for position in self.positions.values():
                if position.unrealized_pnl is not None and position.capital_at_risk is not None:
                    total_loss += position.unrealized_pnl
                    total_capital_at_risk += position.capital_at_risk
            
            if total_capital_at_risk > 0:
                portfolio_loss_percentage = (total_loss / total_capital_at_risk) * 100
                
                if portfolio_loss_percentage < -threshold:
                    self.logger.error(f"🚨 EMERGENCY STOP: Portfolio loss {portfolio_loss_percentage:.2f}% exceeds threshold {threshold}%")
                    return True
            
            return False
            
        except Exception as e:
            self.logger.error(f"Error checking emergency stop: {e}")
            return False

    def validate_position_integrity(self) -> Dict[str, Any]:
        """
        Validate position data integrity and detect anomalies.
        
        Returns:
            Dictionary with validation results and any issues found
        """
        validation_results = {
            'total_positions': len(self.positions),
            'issues': [],
            'warnings': [],
            'anomalies': []
        }
        
        try:
            # Get exchange positions for comparison
            exchange_positions = self.market_api.get_positions()
            exchange_positions_dict = {pos['symbol']: pos for pos in exchange_positions}
            
            for symbol, local_position in self.positions.items():
                # Check if position exists on exchange
                if symbol not in exchange_positions_dict:
                    validation_results['issues'].append(f"Position {symbol} exists locally but not on exchange")
                    continue
                
                exchange_position = exchange_positions_dict[symbol]
                
                # Check size discrepancies
                local_size = local_position.size
                exchange_size = abs(exchange_position['size'])
                size_diff = abs(local_size - exchange_size)
                
                if size_diff > 0.01:  # Allow for small differences
                    validation_results['warnings'].append(
                        f"Size discrepancy for {symbol}: local={local_size}, exchange={exchange_size}"
                    )
                
                # Check price discrepancies
                local_price = local_position.current_price
                exchange_price = exchange_position['mark_price']
                if local_price and exchange_price:
                    price_diff_pct = abs(local_price - exchange_price) / exchange_price * 100
                    if price_diff_pct > 1.0:  # More than 1% difference
                        validation_results['anomalies'].append(
                            f"Price discrepancy for {symbol}: local=${local_price}, exchange=${exchange_price} ({price_diff_pct:.2f}%)"
                        )
                
                # Check for negative sizes (shouldn't happen)
                if local_size < 0:
                    validation_results['issues'].append(f"Negative size for {symbol}: {local_size}")
                
                # Check for unreasonable entry prices
                if local_position.entry_price <= 0:
                    validation_results['issues'].append(f"Invalid entry price for {symbol}: {local_position.entry_price}")
                
                # Check for positions that have been open too long (potential stuck positions)
                time_open_hours = (datetime.now() - local_position.entry_time).total_seconds() / 3600
                if time_open_hours > 168:  # More than 1 week
                    validation_results['warnings'].append(f"Position {symbol} has been open for {time_open_hours:.1f} hours")
            
            # Check for positions on exchange that aren't tracked locally
            local_symbols = set(self.positions.keys())
            exchange_symbols = set(exchange_positions_dict.keys())
            untracked_positions = exchange_symbols - local_symbols
            
            for symbol in untracked_positions:
                validation_results['warnings'].append(f"Position {symbol} exists on exchange but not tracked locally")
            
            validation_results['total_issues'] = len(validation_results['issues'])
            validation_results['total_warnings'] = len(validation_results['warnings'])
            validation_results['total_anomalies'] = len(validation_results['anomalies'])
            
            # Log validation results
            if validation_results['issues']:
                self.logger.error(f"Position validation found {len(validation_results['issues'])} issues")
                for issue in validation_results['issues']:
                    self.logger.error(f"  - {issue}")
            
            if validation_results['warnings']:
                self.logger.warning(f"Position validation found {len(validation_results['warnings'])} warnings")
                for warning in validation_results['warnings']:
                    self.logger.warning(f"  - {warning}")
            
            if validation_results['anomalies']:
                self.logger.warning(f"Position validation found {len(validation_results['anomalies'])} anomalies")
                for anomaly in validation_results['anomalies']:
                    self.logger.warning(f"  - {anomaly}")
            
            return validation_results
            
        except Exception as e:
            self.logger.error(f"Error validating position integrity: {e}")
            validation_results['issues'].append(f"Validation error: {e}")
            return validation_results 