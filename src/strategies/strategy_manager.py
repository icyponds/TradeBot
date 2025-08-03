"""
Strategy manager for orchestrating trading strategies.
"""

import logging
import time
from typing import Dict, List, Any, Optional
from datetime import datetime
import pandas as pd

from ..api.hyperliquid_api import HyperliquidAPI
from ..utils.pair_selector import DynamicPairSelector
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
        self.pair_selector = DynamicPairSelector(config)
        
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
        
        # Configuration
        self.timeframe = config['strategies']['timeframe']
        self.ohlcv_limit = config['strategies']['ohlcv_limit']
        self.max_position_size = config['trading']['max_position_size']
        self.base_currency = config['trading']['base_currency']
        
        # Calculate execution interval based on timeframe
        self.execution_interval = self._get_execution_interval()
        
        self.logger.info(f"Initialized strategy manager with {len(self.strategies)} strategies")
        self.logger.info(f"Timeframe: {self.timeframe}, Execution interval: {self.execution_interval}s")
    
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
    
    def start(self):
        """Start the strategy manager."""
        if self.is_running:
            self.logger.warning("Strategy manager already running")
            return
        
        self.logger.info("Starting strategy manager...")
        
        # Start data collection
        self.market_api.start_data_collection()
        
        # Wait for initial data collection
        self.logger.info("Waiting for initial data collection...")
        time.sleep(10)  # Give WebSocket time to connect and collect data
        
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
                    self._analyze_symbol(symbol)
                
                # Update pair performance
                self.pair_selector.update_pair_performance(trading_pairs)
                
                # Wait for next execution
                time.sleep(self.execution_interval)
                
            except KeyboardInterrupt:
                self.logger.info("Received interrupt signal")
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
            
            self.logger.info(f"{strategy_name} signal for {symbol}: {signal['action']} at {current_price}")
            
            # Check if we should act on the signal
            if self._should_execute_signal(symbol, signal, current_price):
                self._execute_trade(symbol, signal, current_price, strategy_name)
                
        except Exception as e:
            self.logger.error(f"Error executing {strategy_name} strategy for {symbol}: {e}")
    
    def _should_execute_signal(self, symbol: str, signal: Dict[str, Any], current_price: float) -> bool:
        """Determine if we should execute a trading signal."""
        # Check if we already have a position
        if symbol in self.positions:
            position = self.positions[symbol]
            
            # If we have a position, only act on opposite signals
            if position.side == 'long' and signal['action'] == 'buy':
                return False
            elif position.side == 'short' and signal['action'] == 'sell':
                return False
        
        # Check position size limits
        position_value = current_price * signal.get('size', 1.0)
        if position_value > self.max_position_size:
            self.logger.warning(f"Position size {position_value} exceeds limit {self.max_position_size}")
            return False
        
        return True
    
    def _execute_trade(self, symbol: str, signal: Dict[str, Any], current_price: float, strategy_name: str):
        """Execute a trade based on signal."""
        try:
            # Determine trade side
            if signal['action'] == 'buy':
                side = 'buy'
                position_side = 'long'
            elif signal['action'] == 'sell':
                side = 'sell'
                position_side = 'short'
            else:
                return
            
            # Calculate position size
            size = signal.get('size', 1.0)
            position_value = current_price * size
            
            # Place order
            order_result = self.market_api.place_order(symbol, side, size, current_price)
            
            if order_result and order_result.get('status') == 'success':
                # Create trade record
                trade = Trade(
                    symbol=symbol,
                    side=side,
                    size=size,
                    price=current_price,
                    timestamp=datetime.now(),
                    strategy=strategy_name,
                    order_id=order_result.get('order_id'),
                )
                
                # Update position
                position = Position(
                    symbol=symbol,
                    side=position_side,
                    size=size,
                    entry_price=current_price,
                    entry_time=datetime.now(),
                    strategy=strategy_name,
                )
                
                self.positions[symbol] = position
                self.trades.append(trade)
                self.total_trades += 1
                
                self.logger.info(f"Executed {side} trade for {symbol}: {size} @ {current_price}")
                
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
                # Calculate P&L
                if position.side == 'long':
                    pnl = (current_price - position.entry_price) * position.size
                else:
                    pnl = (position.entry_price - current_price) * position.size
                
                # Update performance
                self.total_pnl += pnl
                if pnl > 0:
                    self.winning_trades += 1
                
                # Remove position
                del self.positions[symbol]
                
                self.logger.info(f"Closed position for {symbol}: P&L = {pnl:.2f} {self.base_currency}")
                
            else:
                self.logger.error(f"Failed to close position for {symbol}")
                
        except Exception as e:
            self.logger.error(f"Error closing position for {symbol}: {e}")
    
    def get_performance_summary(self) -> Dict[str, Any]:
        """Get performance summary."""
        # Get pair performance from selector
        pair_performance = self.pair_selector.get_pair_performance_summary()
        
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
        }
    
    def force_pair_rescan(self):
        """Force a rescan of trading pairs."""
        self.pair_selector.force_rescan()
        self.logger.info("Forced pair rescan") 