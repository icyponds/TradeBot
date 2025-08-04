"""
Strategy manager for orchestrating trading strategies.
"""

import logging
import time
import signal
import sys
import json
from typing import Dict, List, Any, Optional
from datetime import datetime, timedelta
import pandas as pd
import math

from src.api.hyperliquid_websocket_api import HyperliquidWebSocketAPI as HyperliquidAPI
from src.utils.pair_selector import DynamicPairSelector
from src.utils.leverage_manager import LeverageManager
from src.utils.portfolio_manager import PortfolioManager
from .moving_average_strategy import MovingAverageStrategy
from .rsi_strategy import RSIStrategy
from src.models.trade import Trade, Position


class StrategyManager:
    """Manages and orchestrates trading strategies."""
    
    def __init__(self, config: Dict[str, Any]):
        """
        Initialize the strategy manager.
        
        Args:
            config: Configuration dictionary
        """
        self.config = config
        self.logger = logging.getLogger(__name__)
        
        # Initialize portfolio manager
        self.portfolio_manager = PortfolioManager(config)
        
        # Initialize leverage manager with portfolio manager
        self.leverage_manager = LeverageManager(config, self.portfolio_manager)
        
        # Initialize market API
        self.market_api = self._initialize_market_api()
        
        # Initialize strategies
        self.strategies = self._initialize_strategies()
        
        # Initialize pair selector
        self.pair_selector = self._initialize_pair_selector()
        
        # Trading configuration
        self.timeframe = config['strategies']['timeframe']
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
        
        # Trading state
        self.positions = {}
        self.trades = []
        self.total_trades = 0
        self.total_pnl = 0.0
        self.winning_trades = 0
        self.is_running = False
        
        self.logger.info(f"Initialized strategy manager with {len(self.strategies)} strategies")
        self.logger.info(f"Timeframe: {self.timeframe}, Execution interval: {self.execution_interval}s")
        self.logger.info(f"Position limit: {self.max_positions_percentage}% of portfolio")
        self.logger.info("Dynamic leverage management enabled")
        
        # Set strategy manager reference in pair selector
        self.pair_selector.strategy_manager = self
        
        # Real-time data subscription tracking
        self._subscribed_symbols = set()
        
        # Real-time price monitoring
        self._price_callbacks = []
        self._last_prices = {}
        
        # Setup signal handlers for kill switch
        self._setup_signal_handlers()
    
    def _setup_signal_handlers(self):
        """Setup signal handlers for kill switch functionality."""
        def signal_handler(signum, frame):
            self.logger.warning(f"Received signal {signum}, initiating emergency stop...")
            self.emergency_stop()
            sys.exit(0)
        
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)
    
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
        Calculate execution interval based on timeframe.
        
        Returns:
            Execution interval in seconds
        """
        timeframe_minutes = {
            '1m': 1, '5m': 5, '15m': 15, '30m': 30,
            '1h': 60, '4h': 240, '1d': 1440
        }
        
        minutes = timeframe_minutes.get(self.timeframe, 60)
        return minutes * 60  # Convert to seconds
    
    def _calculate_signal_strength(self, ohlcv: pd.DataFrame, strategy_name: str) -> float:
        """
        Calculate signal strength based on strategy and market data.
        
        Args:
            ohlcv: OHLCV data
            strategy_name: Name of the strategy
            
        Returns:
            Signal strength (0-1)
        """
        try:
            if strategy_name == 'moving_average':
                # Calculate MA crossover strength
                if len(ohlcv) < 10:
                    return 0.5
                
                short_ma = ohlcv['close'].rolling(window=5).mean()
                long_ma = ohlcv['close'].rolling(window=10).mean()
                
                if len(short_ma) < 2 or len(long_ma) < 2:
                    return 0.5
                
                # Calculate crossover strength
                if long_ma.iloc[-1] <= 0:
                    return 0.5  # Fallback if long MA is invalid
                ma_diff = abs(short_ma.iloc[-1] - long_ma.iloc[-1]) / long_ma.iloc[-1]
                signal_strength = min(1.0, ma_diff * 10)  # Scale to 0-1
                
                return signal_strength
                
            elif strategy_name == 'rsi':
                # Calculate RSI signal strength
                if len(ohlcv) < 14:
                    return 0.5
                
                # Simple RSI calculation
                delta = ohlcv['close'].diff()
                gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
                loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
                
                # Handle division by zero in RSI calculation
                if loss.iloc[-1] == 0:
                    if gain.iloc[-1] > 0:
                        current_rsi = 100.0  # All gains, no losses
                    else:
                        current_rsi = 50.0   # No gains, no losses (neutral)
                else:
                    rs = gain / loss
                    rsi = 100 - (100 / (1 + rs))
                    current_rsi = rsi.iloc[-1]
                
                # current_rsi is already calculated above
                
                # Calculate distance from neutral (50)
                rsi_distance = abs(current_rsi - 50) / 50
                signal_strength = min(1.0, rsi_distance * 2)  # Scale to 0-1
                
                return signal_strength
                
            else:
                return 0.5  # Default signal strength
                
        except Exception as e:
            self.logger.error(f"Error calculating signal strength: {e}")
            return 0.5
    
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
        # Use hybrid API for real-time data + trading operations
        try:
            from src.api.hyperliquid_hybrid_api import HyperliquidHybridAPI
            return HyperliquidHybridAPI(self.config)
        except ImportError:
            # Fallback to SDK API if hybrid not available
            try:
                from src.api.hyperliquid_sdk_api import HyperliquidSDKAPI
                return HyperliquidSDKAPI(self.config)
            except ImportError:
                from src.api.hyperliquid_websocket_api import HyperliquidWebSocketAPI as HyperliquidAPI
                return HyperliquidAPI(self.config)
    
    def _initialize_strategies(self):
        """Initialize trading strategies."""
        return {
            'moving_average': MovingAverageStrategy(self.config),
            'rsi': RSIStrategy(self.config),
        }
    
    def _initialize_pair_selector(self):
        """Initialize the pair selector."""
        return DynamicPairSelector(self.config, self.market_api, self)
    
    def start(self):
        """Start the strategy manager."""
        if self.is_running:
            self.logger.warning("Strategy manager is already running")
            return
        
        self.logger.info("Starting strategy manager...")
        
        # Start market API (only if it has a start method)
        if hasattr(self.market_api, 'start'):
            if not self.market_api.start():
                self.logger.error("Failed to start market API")
                return
        
        # Setup real-time price monitoring
        if hasattr(self.market_api, 'add_price_callback'):
            self.market_api.add_price_callback(self._on_price_update)
            self.logger.info("Real-time price monitoring enabled")
        
        # Initial portfolio update
        self._update_account_balance()
        
        # Update available margin
        self.leverage_manager.update_available_margin(
            self.portfolio_manager.calculate_available_capital_for_trading()
        )
        
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
    
    def stop(self):
        """Stop the strategy manager."""
        if not self.is_running:
            return
        
        self.logger.info("Stopping strategy manager...")
        self.is_running = False
        
        # Stop market API (only if it has a stop method)
        if hasattr(self.market_api, 'stop'):
            self.market_api.stop()
        
        self.logger.info("Strategy manager stopped")
    
    def emergency_stop(self):
        """Emergency stop - close all positions and stop trading."""
        self.logger.warning("EMERGENCY STOP: Closing all positions...")
        try:
            self.close_all_positions("emergency_stop")
            self.stop()
        except Exception as e:
            self.logger.error(f"Error during emergency stop: {e}")
        finally:
            self.logger.info("Emergency stop complete")
    
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
                    if order_id:
                        success = self.market_api.cancel_order(order_id)
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
            else:
                # No open orders to monitor
                pass
                    
        except Exception as e:
            self.logger.error(f"Error monitoring pending orders: {e}")

    def _run_trading_loop(self):
        """Main trading loop."""
        self.logger.info("Starting trading loop...")
        
        # Track last position sync time
        last_position_sync = 0
        last_position_monitoring = 0
        
        # Position monitoring configuration
        position_monitoring_interval = self.config['trading']['position_monitoring_interval']
        position_timeout_hours = self.config['trading']['position_timeout_hours']
        max_loss_percentage = self.config['trading']['max_loss_percentage']
        max_profit_percentage = self.config['trading']['max_profit_percentage']
        emergency_loss_threshold = self.config['trading']['emergency_loss_threshold']
        
        # Statistics
        total_positions_closed = 0
        emergency_stops_triggered = 0
        last_emergency_check = 0
        
        while self.is_running:
            try:
                current_time = time.time()
                
                # With WebSocket, positions are updated in real-time
                # Only sync periodically to ensure accuracy
                if current_time - last_position_sync >= self.position_sync_interval:
                    self.logger.debug(f"Syncing positions with exchange (local: {len(self.positions)})")
                    self.sync_positions_with_exchange()
                    last_position_sync = current_time
                
                # Continuous position monitoring and auto-closure
                if current_time - last_position_monitoring >= position_monitoring_interval:
                    self.logger.debug(f"Running position monitoring check ({len(self.positions)} positions)")
                    self._monitor_and_close_positions(
                        position_timeout_hours, max_loss_percentage, max_profit_percentage,
                        emergency_loss_threshold, total_positions_closed, emergency_stops_triggered,
                        last_emergency_check
                    )
                    last_position_monitoring = current_time
                
                # Validate position integrity (if enabled)
                if self.enable_position_validation:
                    validation_results = self.validate_position_integrity()
                    if validation_results['total_issues'] > 0:
                        self.logger.error(f"Position validation found {validation_results['total_issues']} critical issues")
                
                # Monitor any pending orders
                self._monitor_pending_orders()
                
                # Clean up stale orders
                self._cleanup_stale_orders()
                
                # Get current trading pairs
                trading_pairs = self.pair_selector.get_current_pairs()
                
                if not trading_pairs:
                    self.logger.warning("No trading pairs available")
                    time.sleep(self.execution_interval)
                    continue
                
                self.logger.info(f"Analyzing {len(trading_pairs)} trading pairs")
                
                # Analyze each symbol
                for symbol in trading_pairs:
                    if not self.is_running:
                        break
                    self._analyze_symbol(symbol)
                
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
                
                # Wait for next execution cycle
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
    
    def _analyze_symbol(self, symbol: str):
        """Analyze a single symbol and execute strategies."""
        try:
            # Subscribe to real-time data for this symbol
            self._subscribe_to_symbol(symbol)
            
            # Check if we have sufficient data
            if not self.market_api.is_data_available(symbol):
                self.logger.debug(f"Insufficient data for {symbol}, skipping")
                return
            
            # Get market data
            market_data = self.market_api.get_market_data(symbol, self.timeframe)
            if not market_data:
                self.logger.warning(f"Could not get market data for {symbol}")
                return
            
            # Get OHLCV data
            ohlcv = self.market_api.get_ohlcv(symbol, self.timeframe, self.ohlcv_limit)
            if ohlcv is None or len(ohlcv) < 20:  # Need at least 20 candles for analysis
                self.logger.debug(f"Insufficient OHLCV data for {symbol}")
                return
            
            current_price = market_data['current_price']
            
            # Run each strategy
            for strategy_name, strategy in self.strategies.items():
                self._execute_strategy(symbol, strategy_name, strategy, ohlcv, current_price)
                
        except Exception as e:
            self.logger.error(f"Error analyzing {symbol}: {e}")
    
    def _execute_strategy(self, symbol: str, strategy_name: str, strategy, ohlcv: pd.DataFrame, current_price: float):
        """Execute a single strategy."""
        try:
            # Generate signal
            signal = strategy.generate_signal(ohlcv)
            
            if not signal:
                return
            
            self.logger.info(f"{strategy_name} signal for {symbol}: {signal['signal']} at {current_price}")
            
            # Check if we should act on the signal
            should_execute = self._should_execute_signal(symbol, signal, current_price, ohlcv, strategy_name)
            self.logger.info(f"Should execute {strategy_name} signal for {symbol}: {should_execute}")
            
            if should_execute:
                self.logger.info(f"Executing {strategy_name} trade for {symbol}")
                self._execute_trade(symbol, signal, current_price, strategy_name, ohlcv)
            else:
                self.logger.info(f"Skipping {strategy_name} signal for {symbol} - conditions not met")
                
        except Exception as e:
            self.logger.error(f"Error executing {strategy_name} strategy for {symbol}: {e}")
            import traceback
            self.logger.error(f"Traceback: {traceback.format_exc()}")
    
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
        signal_strength = self._calculate_signal_strength(ohlcv, strategy_name)
        market_volatility = self._calculate_market_volatility(ohlcv)
        
        # Check position limit before proceeding
        if not self._should_execute_with_position_limit(symbol, signal, signal_strength):
            return False
        
        # Calculate dynamic leveraged position size
        available_capital = self.portfolio_manager.calculate_available_capital_for_trading()
        self.logger.info(f"Calculating position size for {symbol}: available capital=${available_capital:.2f}, signal_strength={signal_strength:.2f}, volatility={market_volatility:.2f}")
        
        position_size, margin_required, leverage = self.leverage_manager.calculate_leveraged_position_size(
            symbol, current_price, available_capital, strategy_name, signal_strength, market_volatility
        )
        
        self.logger.info(f"Position calculation for {symbol}: size={position_size:.4f}, margin=${margin_required:.2f}, leverage={leverage:.1f}x")
        
        # Check if we can open the position
        can_open = self.leverage_manager.can_open_position(symbol, margin_required, available_capital)
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
        try:
            # Determine trade side
            if signal['signal'] == 'buy':
                side = 'buy'
                position_side = 'long'
            elif signal['signal'] == 'sell':
                side = 'sell'
                position_side = 'short'
            else:
                return
            
            # Get leverage and position details
            position_size = signal['size']
            leverage = signal['leverage']
            margin_required = signal['margin_required']
            signal_strength = signal['signal_strength']
            market_volatility = signal['market_volatility']
            
            # Calculate stop loss and take profit with leverage
            stop_loss = self.leverage_manager.calculate_stop_loss_with_leverage(
                current_price, position_side, leverage
            )
            
            # Calculate strategy-specific take profit
            strategy = self.strategies[strategy_name]
            strategy_take_profit = strategy.calculate_take_profit(
                current_price, position_side, ohlcv, signal_strength, market_volatility
            )
            
            take_profit = self.leverage_manager.calculate_take_profit_with_leverage(
                current_price, position_side, leverage, strategy_take_profit
            )
            
            # Alternative: Calculate stop-loss and take-profit based on capital at risk
            # This gives us more precise control over dollar amounts at risk
            max_loss_amount = margin_required * 0.5  # 50% of capital at risk as max loss
            target_profit_amount = margin_required * 1.0  # 100% of capital at risk as target profit
            
            stop_loss_capital_based = self.leverage_manager.calculate_stop_loss_with_capital_at_risk(
                current_price, position_side, margin_required, max_loss_amount
            )
            
            take_profit_capital_based = self.leverage_manager.calculate_take_profit_with_capital_at_risk(
                current_price, position_side, margin_required, target_profit_amount
            )
            
            # Use the more conservative of the two approaches
            if position_side == 'long':
                stop_loss = max(stop_loss, stop_loss_capital_based)  # Higher price = more conservative
                take_profit = min(take_profit, take_profit_capital_based)  # Lower price = more conservative
            else:
                stop_loss = min(stop_loss, stop_loss_capital_based)  # Lower price = more conservative
                take_profit = max(take_profit, take_profit_capital_based)  # Higher price = more conservative
            
            # Place order with current price to ensure proper order ID
            current_price = self.market_api.get_current_price(symbol)
            if not current_price:
                self.logger.error(f"Could not get current price for {symbol}")
                return
                
            order_result = self.market_api.place_order(symbol, side, position_size, current_price)
            
            if order_result and order_result.get('status') == 'pending':
                order_id = order_result.get('order_id')
                self.logger.info(f"Placed {side} order for {symbol}: {position_size} @ {current_price} (Order ID: {order_id})")
                
                # Check if order was immediately filled by examining the response
                order_response = order_result.get('order_response', {})
                is_filled = False
                fill_price = current_price
                fill_size = position_size
                
                # Check the order response structure for fill status
                if order_response and 'response' in order_response:
                    response_data = order_response['response']
                    if 'data' in response_data and 'statuses' in response_data['data']:
                        statuses = response_data['data']['statuses']
                        if statuses and len(statuses) > 0:
                            status = statuses[0]
                            if 'filled' in status:
                                is_filled = True
                                fill_data = status['filled']
                                fill_size = float(fill_data.get('totalSz', position_size))
                                fill_price = float(fill_data.get('avgPx', current_price))
                                self.logger.info(f"✅ Order filled immediately: {symbol} {fill_size} @ {fill_price}")
                            elif 'resting' in status:
                                self.logger.info(f"⏳ Order resting on books: {symbol}")
                            elif 'error' in status:
                                self.logger.error(f"❌ Order error: {status['error']}")
                                return
                
                # If not immediately filled, wait and check exchange positions
                if not is_filled:
                    # Wait a moment for order to potentially fill
                    time.sleep(2)
                    
                    # Check if position actually exists on exchange
                    exchange_positions = self.market_api.get_positions()
                    actual_position = next((pos for pos in exchange_positions if pos['symbol'] == symbol), None)
                    
                    if actual_position:
                        # Position exists on exchange - use actual fill data
                        fill_price = actual_position['entry_price']
                        fill_size = abs(actual_position['size'])
                        is_filled = True
                        self.logger.info(f"✅ Order filled: {symbol} {fill_size} @ {fill_price}")
                    else:
                        # Position doesn't exist - order didn't fill
                        self.logger.warning(f"❌ Order for {symbol} did not fill - not recording position")
                        return
                
                # Only proceed if order was filled
                if is_filled:
                    # Create trade record with order data
                    trade = Trade(
                        symbol=symbol,
                        side=side,
                        size=fill_size,
                        price=fill_price,
                        timestamp=datetime.now(),
                        strategy=strategy_name,
                        order_id=order_id,
                    )
                    
                    # Update position with actual fill data
                    position = Position(
                        symbol=symbol,
                        side=position_side,
                        size=fill_size,
                        entry_price=fill_price,
                        entry_time=datetime.now(),
                        strategy=strategy_name,
                        stop_loss=stop_loss,
                        take_profit=take_profit,
                        capital_at_risk=margin_required,  # Set the actual capital at risk
                    )
                    
                    # Record position in leverage manager
                    self.leverage_manager.record_position(
                        symbol, position_side, fill_size, fill_price, leverage, margin_required
                    )
                    
                    self.positions[symbol] = position
                    self.trades.append(trade)
                    self.total_trades += 1
                    
                    # Save positions to file for kill switch access
                    self.save_positions_to_file()
                    
                    self.logger.info(f"✅ Executed {side} trade for {symbol}: {fill_size} @ {fill_price} with {leverage:.1f}x leverage")
                    self.logger.info(f"Signal strength: {signal_strength:.2f}, Volatility: {market_volatility:.2f}")
                    self.logger.info(f"Stop loss: {stop_loss:.2f}, Take profit: {take_profit:.2f}")
                    
                    # Verify position was recorded correctly
                    exchange_positions = self.market_api.get_positions()
                    self.logger.info(f"📊 Position recorded: {len(self.positions)} local positions, {len(exchange_positions)} exchange positions")
                
            else:
                self.logger.error(f"Failed to place order for {symbol}")
                
        except Exception as e:
            self.logger.error(f"Error executing trade for {symbol}: {e}")
    
    def close_position(self, symbol: str, reason: str = "manual"):
        """Close a position."""
        if symbol not in self.positions:
            self.logger.warning(f"No position to close for {symbol}")
            return
        
        try:
            position = self.positions[symbol]
            current_price = self.market_api.get_current_price(symbol)
            
            if not current_price:
                self.logger.error(f"Could not get current price for {symbol}")
                return
            
            # Determine close side
            close_side = 'sell' if position.side == 'long' else 'buy'
            
            # Place close order
            order_result = self.market_api.place_order(symbol, close_side, position.size, current_price)
            
            if order_result and order_result.get('status') in ['success', 'pending']:
                # For kill switch, we'll assume the order was successful
                # In a production environment, you'd want to wait for confirmation
                order_id = order_result.get('order_id')
                if order_id:
                    self.logger.info(f"Close order placed for {symbol}: {order_id}")
                # Close position in leverage manager
                result = self.leverage_manager.close_position(symbol, current_price)
                
                if result:
                    # Update performance
                    self.total_pnl += result['leveraged_pnl']
                    if result['leveraged_pnl'] > 0:
                        self.winning_trades += 1
                    
                    # Remove position
                    del self.positions[symbol]
                    
                    # Save positions to file for kill switch access
                    self.save_positions_to_file()
                    
                    self.logger.info(f"Closed position for {symbol}: {result['leveraged_pnl']:.2f} USDC ({result['leveraged_pnl_percentage']:.2f}%)")
                
            else:
                self.logger.error(f"Failed to close position for {symbol}")
                
        except Exception as e:
            self.logger.error(f"Error closing position for {symbol}: {e}")
    
    def close_all_positions(self, reason: str = "kill_switch"):
        """Close all open positions."""
        self.logger.info(f"Closing all positions due to {reason}...")
        symbols_to_close = list(self.positions.keys())
        closed_count = 0
        
        for symbol in symbols_to_close:
            try:
                # Try to close position with timeout
                self.close_position(symbol, reason)
                closed_count += 1
                time.sleep(0.5)  # Small delay between orders to avoid rate limiting
            except Exception as e:
                self.logger.error(f"Error closing position for {symbol}: {e}")
        
        self.logger.info(f"Attempted to close {len(symbols_to_close)} positions, successfully closed {closed_count}.")
    
    def get_performance_summary(self) -> Dict[str, Any]:
        """Get performance summary."""
        # Get pair performance from selector
        pair_performance = self.pair_selector.get_pair_performance_summary()
        
        # Get margin and risk summaries
        margin_summary = self.leverage_manager.get_margin_summary()
        risk_summary = self.leverage_manager.get_risk_summary()
        
        # Calculate win rate
        win_rate = (self.winning_trades / self.total_trades * 100) if self.total_trades > 0 else 0
        
        return {
            'total_trades': self.total_trades,
            'winning_trades': self.winning_trades,
            'win_rate': win_rate,
            'total_pnl': self.total_pnl,
            'open_positions': len(self.positions),
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
    
    def _check_position_limit(self) -> bool:
        """
        Check if we've reached the position limit based on capital at risk.
        
        Returns:
            True if position limit reached, False otherwise
        """
        # Check portfolio allocation to see if we're at the limit
        allocation = self._check_portfolio_allocation()
        current_allocation = allocation['allocation_percentage']
        
        # Log the current allocation status
        self.logger.info(f"Position limit check: {len(self.positions)} positions, {current_allocation:.1f}% of portfolio at risk (max: {self.max_positions_percentage}%)")
        
        # Return True if we're at or exceeding the allocation limit
        return current_allocation >= self.max_positions_percentage
    
    def _get_position_profitability_score(self, symbol: str, new_signal_strength: float) -> float:
        """
        Calculate a profitability score for a position to determine if it should be closed.
        
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
        
        # Calculate unrealized PnL percentage
        pnl_percentage = position.unrealized_pnl_percentage or 0.0
        
        # Calculate time factor (older positions get lower scores)
        time_open = (datetime.now() - position.entry_time).total_seconds() / 3600  # hours
        time_factor = max(0.1, 1.0 - (time_open / 24))  # Decay over 24 hours
        
        # Calculate signal strength factor
        signal_factor = 0.5  # Default for existing positions
        
        # Calculate overall score
        score = (pnl_percentage * 0.4) + (time_factor * 0.3) + (signal_factor * 0.3)
        
        return score
    
    def _close_least_profitable_position(self, new_signal_strength: float) -> bool:
        """
        Close the least profitable position to make room for a new trade.
        
        Args:
            new_signal_strength: Signal strength of the new potential trade
            
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
        
        # Find the position with the lowest score
        least_profitable_symbol = min(position_scores.keys(), key=lambda x: position_scores[x])
        least_profitable_score = position_scores[least_profitable_symbol]
        
        # If we're at the position limit, be more aggressive about closing positions
        if self._check_position_limit():
            # Close the least profitable position if the new signal is stronger
            if new_signal_strength > least_profitable_score * 1.2:  # 20% stronger when at limit
                self.logger.info(f"Position limit reached - closing least profitable position {least_profitable_symbol} (score: {least_profitable_score:.2f}) for new trade (strength: {new_signal_strength:.2f})")
                self.close_position(least_profitable_symbol, "position_limit")
                return True
        else:
            # Normal mode - only close if significantly stronger
            if new_signal_strength > least_profitable_score * 1.5:  # 50% stronger
                self.logger.info(f"Closing least profitable position {least_profitable_symbol} (score: {least_profitable_score:.2f}) for new trade (strength: {new_signal_strength:.2f})")
                self.close_position(least_profitable_symbol, "position_limit")
                return True
        
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
        self.logger.info(f"Portfolio allocation for {symbol}: {allocation['allocation_percentage']:.1f}% (max: {self.max_positions_percentage}%)")
        
        if allocation['allocation_percentage'] >= self.max_positions_percentage:
            self.logger.warning(f"Portfolio allocation limit reached: {allocation['allocation_percentage']:.1f}% >= {self.max_positions_percentage}%")
            # Try to close least profitable position to make room
            if self._close_least_profitable_position(signal_strength):
                self.logger.info(f"Closed least profitable position to make room for {symbol}")
                return True
            return False
        
        # If we haven't reached the position count limit, allow the trade
        position_limit_reached = self._check_position_limit()
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

    def emergency_stop(self):
        """Emergency stop - close all positions and stop trading."""
        self.logger.warning("EMERGENCY STOP: Closing all positions...")
        try:
            self.close_all_positions("emergency_stop")
            self.stop()
        except Exception as e:
            self.logger.error(f"Error during emergency stop: {e}")
        finally:
            self.logger.info("Emergency stop complete")
    
    def save_positions_to_file(self):
        """Save current positions to a JSON file."""
        positions_data = {}
        for symbol, position in self.positions.items():
            positions_data[symbol] = {
                'side': position.side,
                'size': position.size,
                'entry_price': position.entry_price,
                'entry_time': position.entry_time.isoformat(),
                'strategy': position.strategy,
                'stop_loss': position.stop_loss,
                'take_profit': position.take_profit,
                'capital_at_risk': position.capital_at_risk, # Save capital at risk
            }
        
        try:
            with open('positions.json', 'w') as f:
                json.dump(positions_data, f, indent=2, default=str)
            self.logger.info(f"Saved {len(positions_data)} positions to positions.json")
        except Exception as e:
            self.logger.error(f"Failed to save positions: {e}")
    
    def load_positions_from_file(self):
        """Load positions from JSON file."""
        try:
            with open('positions.json', 'r') as f:
                positions_data = json.load(f)
            
            for symbol, pos_data in positions_data.items():
                # Convert entry_time back to datetime
                entry_time = datetime.fromisoformat(pos_data['entry_time'])
                
                position = Position(
                    symbol=symbol,
                    side=pos_data['side'],
                    size=pos_data['size'],
                    entry_price=pos_data['entry_price'],
                    entry_time=entry_time,
                    strategy=pos_data['strategy'],
                    stop_loss=pos_data.get('stop_loss'),
                    take_profit=pos_data.get('take_profit'),
                    capital_at_risk=pos_data.get('capital_at_risk'),  # Load capital at risk
                )
                self.positions[symbol] = position
            
            self.logger.info(f"Loaded {len(positions_data)} positions from positions.json")
            return True
        except FileNotFoundError:
            self.logger.info("No positions.json file found")
            return False
        except Exception as e:
            self.logger.error(f"Failed to load positions: {e}")
            return False
    
    def update_position_prices(self):
        """Update current prices for all open positions."""
        for symbol, position in self.positions.items():
            current_price = self.market_api.get_current_price(symbol)
            if current_price:
                position.current_price = current_price
    
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
        
        return {
            'positions': positions_summary,
            'total_positions': len(positions_summary),
            'total_unrealized_pnl': total_unrealized_pnl,
            'total_capital_at_risk': total_capital_at_risk,
            'average_pnl_percentage': (total_unrealized_pnl / total_capital_at_risk * 100) if total_capital_at_risk > 0 else 0,
        }
    
    def display_positions_pnl(self):
        """Display current positions with PnL information."""
        if not self.positions:
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
        
        self.logger.info("-" * 60)
        self.logger.info(f"TOTAL: {len(positions_summary['positions'])} positions | PnL: ${positions_summary['total_unrealized_pnl']:>8.2f} | Capital at Risk: ${allocation['total_capital_at_risk']:>8.2f}")
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
                    
                    if self.market_api.cancel_order(order_id):
                        self.logger.info(f"Successfully cancelled order {order_id}")
                    else:
                        self.logger.warning(f"Failed to cancel order {order_id}")
                
                # Wait a moment for cancellations to process
                time.sleep(2)
            else:
                self.logger.info("No open orders found")
                
        except Exception as e:
            self.logger.error(f"Error cleaning up open orders: {e}") 

    def _monitor_and_close_positions(self, timeout_hours: float, max_loss_percentage: float, 
                                   max_profit_percentage: float, emergency_threshold: float,
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
                
                close_reason = self._should_close_position(position, timeout_hours, max_loss_percentage, max_profit_percentage)
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
    
    def _should_close_position(self, position: Position, timeout_hours: float, 
                             max_loss_percentage: float, max_profit_percentage: float) -> Optional[str]:
        """
        Determine if a position should be closed.
        
        Args:
            position: Position to check
            timeout_hours: Hours before position timeout
            max_loss_percentage: Maximum loss percentage
            max_profit_percentage: Maximum profit percentage
            
        Returns:
            Reason for closure if should close, None otherwise
        """
        if not position.current_price:
            return None
        
        # Check stop loss
        if position.stop_loss:
            if position.side == 'long' and position.current_price <= position.stop_loss:
                return "stop_loss"
            elif position.side == 'short' and position.current_price >= position.stop_loss:
                return "stop_loss"
        
        # Check take profit
        if position.take_profit:
            if position.side == 'long' and position.current_price >= position.take_profit:
                return "take_profit"
            elif position.side == 'short' and position.current_price <= position.take_profit:
                return "take_profit"
        
        # Check position timeout
        time_open = datetime.now() - position.entry_time
        if time_open.total_seconds() > (timeout_hours * 3600):
            return "timeout"
        
        # Check loss percentage
        if position.unrealized_pnl_percentage:
            if position.unrealized_pnl_percentage < -max_loss_percentage:
                return "max_loss"
        
        # Check profit percentage
        if position.unrealized_pnl_percentage:
            if position.unrealized_pnl_percentage > max_profit_percentage:
                return "max_profit"
        
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