"""
Strategy manager for orchestrating trading strategies.
"""

import logging
import time
import signal
import sys
import json
from typing import Dict, List, Any, Optional
from datetime import datetime
import pandas as pd
import math

from ..api.hyperliquid_sdk_api import HyperliquidSDKAPI as HyperliquidAPI
from ..utils.pair_selector import DynamicPairSelector
from ..utils.leverage_manager import LeverageManager
from .moving_average_strategy import MovingAverageStrategy
from .rsi_strategy import RSIStrategy
from ..models.trade import Trade, Position


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
        
        # Initialize API client
        self.market_api = HyperliquidAPI(config)
        
        # Initialize pair selector
        self.pair_selector = DynamicPairSelector(config, self.market_api)
        
        # Initialize leverage manager
        self.leverage_manager = LeverageManager(config)
        
        # Initialize strategies
        self.strategies = {
            'moving_average': MovingAverageStrategy(config),
            'rsi': RSIStrategy(config),
        }
        
        # Trading state
        self.positions = {}  # symbol -> Position
        self.trades = []  # List of completed trades
        self.is_running = False
        
        # Performance tracking
        self.total_pnl = 0.0
        self.winning_trades = 0
        self.total_trades = 0
        self.available_capital = 1000.0  # Default capital, should be fetched from API
        
        # Configuration
        self.timeframe = config['strategies']['timeframe']
        self.ohlcv_limit = config['strategies']['ohlcv_limit']
        self.max_position_size = config['trading']['max_position_size']
        self.max_positions_percentage = config['trading']['max_positions_percentage']
        self.base_currency = config['trading']['base_currency']
        
        # Trading pairs management
        self.max_pairs_to_trade = 20  # Default value, can be updated dynamically
        
        # Calculate execution interval based on timeframe
        self.execution_interval = self._get_execution_interval()
        
        self.logger.info(f"Initialized strategy manager with {len(self.strategies)} strategies")
        self.logger.info(f"Timeframe: {self.timeframe}, Execution interval: {self.execution_interval}s")
        self.logger.info(f"Position limit: {self.max_positions_percentage}% of portfolio")
        self.logger.info("Dynamic leverage management enabled")
        
        # Set strategy manager reference in pair selector
        self.pair_selector.strategy_manager = self
        
        # Setup signal handlers for kill switch
        self._setup_signal_handlers()
    
    def _setup_signal_handlers(self):
        """Setup signal handlers for kill switch functionality."""
        def signal_handler(signum, frame):
            self.logger.warning(f"Received signal {signum}, activating kill switch...")
            self.close_all_positions("kill_switch")
            self.stop()
            sys.exit(0)
        
        # Register signal handlers
        signal.signal(signal.SIGINT, signal_handler)   # Ctrl+C
        signal.signal(signal.SIGTERM, signal_handler)  # Termination signal
        
        self.logger.info("Kill switch enabled: Press Ctrl+C to close all positions and exit")
    
    def _get_execution_interval(self) -> int:
        """Calculate execution interval based on timeframe."""
        timeframe_intervals = {
            '1m': 30,    # Execute every 30 seconds for 1m timeframe
            '5m': 60,    # Execute every 1 minute for 5m timeframe
            '15m': 300,  # Execute every 5 minutes for 15m timeframe
            '30m': 600,  # Execute every 10 minutes for 30m timeframe
            '1h': 1800,  # Execute every 30 minutes for 1h timeframe
            '4h': 3600,  # Execute every hour for 4h timeframe
            '1d': 14400, # Execute every 4 hours for 1d timeframe
        }
        return timeframe_intervals.get(self.timeframe, 60)
    
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
                rs = gain / loss
                rsi = 100 - (100 / (1 + rs))
                
                if len(rsi) < 1:
                    return 0.5
                
                current_rsi = rsi.iloc[-1]
                
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
    
    def start(self):
        """Start the strategy manager."""
        if self.is_running:
            self.logger.warning("Strategy manager already running")
            return
        
        self.logger.info("Starting strategy manager...")
        
        # Load existing positions from file
        self.load_positions_from_file()
        
        # Start data collection
        self.market_api.start_data_collection()
        
        # Wait for initial data collection
        self.logger.info("Waiting for initial data collection...")
        time.sleep(10)  # Give WebSocket time to connect and collect data
        
        # Update available margin
        self.leverage_manager.update_available_margin(self.available_capital)
        
        self.is_running = True
        
        # Start the main trading loop
        self._run_trading_loop()
    
    def stop(self):
        """Stop the strategy manager."""
        self.is_running = False
        self.market_api.stop_data_collection()
        self.logger.info("Strategy manager stopped")
    
    def _run_trading_loop(self):
        """Main trading loop."""
        self.logger.info("Starting trading loop...")
        
        while self.is_running:
            try:
                # Get current trading pairs
                trading_pairs = self.pair_selector.get_current_pairs()
                
                if not trading_pairs:
                    self.logger.warning("No trading pairs available")
                    time.sleep(self.execution_interval)
                    continue
                
                self.logger.info(f"Analyzing {len(trading_pairs)} trading pairs")
                
                # Analyze each pair
                for symbol in trading_pairs:
                    if not self.is_running:  # Check if kill switch was activated
                        break
                    self._analyze_symbol(symbol)
                
                # Update pair performance (tracking but not updating PnL yet)
                # self.pair_selector.update_pair_performance(trading_pairs)
                
                # Log margin summary
                margin_summary = self.leverage_manager.get_margin_summary()
                self.logger.info(f"Margin usage: {margin_summary['margin_utilization']:.1f}% ({margin_summary['open_positions']} positions)")
                
                # Display positions PnL if we have open positions
                if self.positions:
                    self.display_positions_pnl()
                
                # Wait for next execution
                time.sleep(self.execution_interval)
                
            except KeyboardInterrupt:
                self.logger.info("Received interrupt signal")
                self.close_all_positions("keyboard_interrupt")
                break
            except Exception as e:
                self.logger.error(f"Error in trading loop: {e}")
                time.sleep(self.execution_interval)
    
    def _analyze_symbol(self, symbol: str):
        """Analyze a single symbol and execute strategies."""
        try:
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
            if self._should_execute_signal(symbol, signal, current_price, ohlcv, strategy_name):
                self._execute_trade(symbol, signal, current_price, strategy_name, ohlcv)
                
        except Exception as e:
            self.logger.error(f"Error executing {strategy_name} strategy for {symbol}: {e}")
    
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
        position_size, margin_required, leverage = self.leverage_manager.calculate_leveraged_position_size(
            symbol, current_price, self.available_capital, strategy_name, signal_strength, market_volatility
        )
        
        # Check if we can open the position
        if not self.leverage_manager.can_open_position(symbol, margin_required, self.available_capital):
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
            
            # Place order
            order_result = self.market_api.place_order(symbol, side, position_size, current_price)
            
            if order_result and order_result.get('status') == 'success':
                # Create trade record
                trade = Trade(
                    symbol=symbol,
                    side=side,
                    size=position_size,
                    price=current_price,
                    timestamp=datetime.now(),
                    strategy=strategy_name,
                    order_id=order_result.get('order_id'),
                )
                
                # Update position
                position = Position(
                    symbol=symbol,
                    side=position_side,
                    size=position_size,
                    entry_price=current_price,
                    entry_time=datetime.now(),
                    strategy=strategy_name,
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                )
                
                # Record position in leverage manager
                self.leverage_manager.record_position(
                    symbol, position_side, position_size, current_price, leverage, margin_required
                )
                
                self.positions[symbol] = position
                self.trades.append(trade)
                self.total_trades += 1
                
                # Save positions to file for kill switch access
                self.save_positions_to_file()
                
                self.logger.info(f"Executed {side} trade for {symbol}: {position_size} @ {current_price} with {leverage:.1f}x leverage")
                self.logger.info(f"Signal strength: {signal_strength:.2f}, Volatility: {market_volatility:.2f}")
                self.logger.info(f"Stop loss: {stop_loss:.2f}, Take profit: {take_profit:.2f}")
                
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
            
            if order_result and order_result.get('status') == 'success':
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
        for symbol in symbols_to_close:
            self.close_position(symbol, reason)
        self.logger.info(f"Closed {len(symbols_to_close)} positions.")
    
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
        Check if we've reached the position limit.
        
        Returns:
            True if position limit reached, False otherwise
        """
        current_positions = len(self.positions)
        max_positions = int(self.available_capital * (self.max_positions_percentage / 100))
        
        self.logger.info(f"Position limit check: {current_positions}/{max_positions} positions")
        return current_positions >= max_positions
    
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
        
        # Only close if the new signal is significantly stronger
        if new_signal_strength > least_profitable_score * 1.5:  # 50% stronger
            self.logger.info(f"Closing least profitable position {least_profitable_symbol} (score: {least_profitable_score:.2f}) for new trade (strength: {new_signal_strength:.2f})")
            self.close_position(least_profitable_symbol, "position_limit")
            return True
        
        return False
    
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
        # If we haven't reached the limit, allow the trade
        if not self._check_position_limit():
            return True
        
        # If we have reached the limit, try to close a less profitable position
        if self._close_least_profitable_position(signal_strength):
            return True
        
        # If we couldn't close any positions, don't execute the trade
        self.logger.warning(f"Position limit reached ({len(self.positions)} positions), skipping trade for {symbol}")
        return False 

    def emergency_stop(self):
        """Emergency stop - close all positions and stop the bot."""
        self.logger.warning("EMERGENCY STOP ACTIVATED - Closing all positions!")
        self.close_all_positions("emergency_stop")
        self.stop()
        self.logger.info("Emergency stop completed")
    
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
        total_position_value = 0.0
        
        for symbol, position in self.positions.items():
            if position.current_price is None:
                continue
            
            unrealized_pnl = position.unrealized_pnl
            unrealized_pnl_percentage = position.unrealized_pnl_percentage
            position_value = position.current_price * position.size
            
            if unrealized_pnl is not None:
                total_unrealized_pnl += unrealized_pnl
                total_position_value += position_value
            
            positions_summary.append({
                'symbol': symbol,
                'side': position.side,
                'size': position.size,
                'entry_price': position.entry_price,
                'current_price': position.current_price,
                'unrealized_pnl': unrealized_pnl,
                'unrealized_pnl_percentage': unrealized_pnl_percentage,
                'position_value': position_value,
                'strategy': position.strategy,
                'time_open': (datetime.now() - position.entry_time).total_seconds() / 3600,  # hours
            })
        
        return {
            'positions': positions_summary,
            'total_positions': len(positions_summary),
            'total_unrealized_pnl': total_unrealized_pnl,
            'total_position_value': total_position_value,
            'average_pnl_percentage': (total_unrealized_pnl / total_position_value * 100) if total_position_value > 0 else 0,
        }
    
    def display_positions_pnl(self):
        """Display current PnL for all open positions."""
        summary = self.get_positions_summary()
        
        if not summary['positions']:
            self.logger.info("No open positions")
            return
        
        self.logger.info("=" * 60)
        self.logger.info("📊 OPEN POSITIONS SUMMARY")
        self.logger.info("=" * 60)
        
        # Sort by PnL (best performing first)
        positions = sorted(summary['positions'], key=lambda x: x['unrealized_pnl'] or 0, reverse=True)
        
        for pos in positions:
            pnl = pos['unrealized_pnl'] or 0
            pnl_pct = pos['unrealized_pnl_percentage'] or 0
            time_open = pos['time_open']
            
            # Color coding for PnL
            if pnl > 0:
                status = "🟢 PROFIT"
            elif pnl < 0:
                status = "🔴 LOSS"
            else:
                status = "⚪ BREAKEVEN"
            
            self.logger.info(
                f"{pos['symbol']:<8} {pos['side'].upper():<6} {pos['size']:>8.3f} @ "
                f"${pos['entry_price']:<8.4f} → ${pos['current_price']:<8.4f} | "
                f"PnL: ${pnl:>8.2f} ({pnl_pct:>6.2f}%) | "
                f"Time: {time_open:>5.1f}h | {status}"
            )
        
        self.logger.info("-" * 60)
        self.logger.info(
            f"TOTAL: {summary['total_positions']} positions | "
            f"PnL: ${summary['total_unrealized_pnl']:>8.2f} "
            f"({summary['average_pnl_percentage']:>6.2f}%) | "
            f"Value: ${summary['total_position_value']:>8.2f}"
        )
        self.logger.info("=" * 60) 