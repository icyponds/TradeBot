"""
Strategy Manager for coordinating multiple trading strategies.
"""

import logging
import time
from typing import Dict, Any, List
from datetime import datetime

from .moving_average_strategy import MovingAverageStrategy
from .rsi_strategy import RSIStrategy
from ..utils.pair_selector import DynamicPairSelector


class StrategyManager:
    """Manages multiple trading strategies and coordinates their execution."""
    
    def __init__(self, config: Dict[str, Any], market_api):
        """
        Initialize the strategy manager.
        
        Args:
            config: Configuration dictionary
            market_api: Market data API instance
        """
        self.config = config
        self.market_api = market_api
        self.logger = logging.getLogger(__name__)
        
        # Initialize dynamic pair selector
        self.pair_selector = DynamicPairSelector(config, market_api)
        
        # Initialize strategies
        self.strategies = {}
        self.enabled_strategies = config['strategies']['enabled']
        
        # Get timeframe configuration
        self.timeframe = config['strategies']['timeframe']
        self.ohlcv_limit = config['strategies']['ohlcv_limit']
        
        # Set execution frequency based on timeframe
        self.execution_interval = self._get_execution_interval()
        
        self._initialize_strategies()
        
        # Trading state
        self.is_running = False
        self.positions = {}
        self.trades = []
        
        self.logger.info(f"Strategy manager initialized with {self.timeframe} timeframe, executing every {self.execution_interval} seconds")
    
    def _get_execution_interval(self) -> int:
        """
        Get execution interval based on timeframe.
        
        Returns:
            Execution interval in seconds
        """
        timeframe_intervals = {
            '1m': 30,    # Execute every 30 seconds for 1m timeframe
            '5m': 60,    # Execute every 60 seconds for 5m timeframe
            '15m': 120,  # Execute every 2 minutes for 15m timeframe
            '30m': 300,  # Execute every 5 minutes for 30m timeframe
            '1h': 600,   # Execute every 10 minutes for 1h timeframe
            '4h': 1800,  # Execute every 30 minutes for 4h timeframe
            '1d': 3600,  # Execute every hour for 1d timeframe
        }
        
        return timeframe_intervals.get(self.timeframe, 60)
    
    def _initialize_strategies(self):
        """Initialize all enabled strategies."""
        strategy_classes = {
            'moving_average': MovingAverageStrategy,
            'rsi': RSIStrategy,
        }
        
        for strategy_name in self.enabled_strategies:
            if strategy_name in strategy_classes:
                try:
                    strategy_class = strategy_classes[strategy_name]
                    self.strategies[strategy_name] = strategy_class(self.config, self.market_api)
                    self.logger.info(f"Initialized strategy: {strategy_name}")
                except Exception as e:
                    self.logger.error(f"Failed to initialize strategy {strategy_name}: {e}")
            else:
                self.logger.warning(f"Unknown strategy: {strategy_name}")
    
    def run(self):
        """Run the strategy manager in a continuous loop."""
        self.is_running = True
        self.logger.info("Starting strategy manager...")
        
        # Test API connection
        if not self.market_api.test_connection():
            self.logger.error("Failed to connect to market API")
            return
        
        try:
            while self.is_running:
                self._execute_trading_cycle()
                time.sleep(self.execution_interval)  # Dynamic interval based on timeframe
                
        except KeyboardInterrupt:
            self.logger.info("Strategy manager stopped by user")
        except Exception as e:
            self.logger.error(f"Error in strategy manager: {e}")
        finally:
            self.stop()
    
    def _execute_trading_cycle(self):
        """Execute one trading cycle for all strategies."""
        self.logger.info("Executing trading cycle...")
        
        # Get current trading pairs from dynamic selector
        trading_pairs = self.pair_selector.get_current_pairs()
        
        if not trading_pairs:
            self.logger.warning("No trading pairs selected")
            return
        
        self.logger.info(f"Trading {len(trading_pairs)} pairs: {trading_pairs}")
        
        # Get market data for all selected symbols
        for symbol in trading_pairs:
            try:
                # Get market data with configured timeframe
                market_data = self.market_api.get_market_data(symbol, self.timeframe)
                if market_data is None:
                    self.logger.warning(f"Failed to get market data for {symbol}")
                    continue
                
                # Execute each strategy
                for strategy_name, strategy in self.strategies.items():
                    if not strategy.is_active:
                        continue
                    
                    self._execute_strategy(strategy_name, strategy, market_data)
                    
            except Exception as e:
                self.logger.error(f"Error processing {symbol}: {e}")
    
    def _execute_strategy(self, strategy_name: str, strategy, market_data: Dict[str, Any]):
        """
        Execute a single strategy.
        
        Args:
            strategy_name: Name of the strategy
            strategy: Strategy instance
            market_data: Market data dictionary
        """
        try:
            # Analyze market data
            analysis = strategy.analyze(market_data)
            
            symbol = market_data['symbol']
            current_price = market_data['current_price']
            
            # Check for buy signal
            if strategy.should_buy(analysis):
                self._execute_buy_signal(strategy_name, strategy, symbol, current_price, analysis)
            
            # Check for sell signal
            elif strategy.should_sell(analysis):
                self._execute_sell_signal(strategy_name, strategy, symbol, current_price, analysis)
            
            # Log analysis results
            self.logger.info(f"{strategy_name} - {symbol}: {analysis.get('reason', 'No signal')}")
            
        except Exception as e:
            self.logger.error(f"Error executing strategy {strategy_name}: {e}")
    
    def _execute_buy_signal(self, strategy_name: str, strategy, symbol: str, price: float, analysis: Dict[str, Any]):
        """Execute a buy signal."""
        # Check if we already have a position
        position_key = f"{strategy_name}_{symbol}"
        
        if position_key in self.positions:
            self.logger.info(f"Already have position in {symbol} for {strategy_name}")
            return
        
        # Calculate position size
        risk_amount = self.config['trading']['max_position_size'] * 0.1  # 10% of max position
        position_size = strategy.calculate_position_size(price, risk_amount)
        
        # Calculate stop loss and take profit
        stop_loss = strategy.calculate_stop_loss(price, 'buy')
        take_profit = strategy.calculate_take_profit(price, 'buy')
        
        # Record the trade
        trade = {
            'strategy': strategy_name,
            'symbol': symbol,
            'side': 'buy',
            'price': price,
            'size': position_size,
            'stop_loss': stop_loss,
            'take_profit': take_profit,
            'timestamp': datetime.now(),
            'analysis': analysis,
        }
        
        self.positions[position_key] = trade
        self.trades.append(trade)
        
        # Record in strategy
        strategy.record_trade(symbol, 'buy', price, position_size, datetime.now())
        
        self.logger.info(f"BUY signal executed: {symbol} at {price:.2f} (size: {position_size:.2f})")
    
    def _execute_sell_signal(self, strategy_name: str, strategy, symbol: str, price: float, analysis: Dict[str, Any]):
        """Execute a sell signal."""
        position_key = f"{strategy_name}_{symbol}"
        
        if position_key not in self.positions:
            self.logger.info(f"No position to sell in {symbol} for {strategy_name}")
            return
        
        # Get the original position
        position = self.positions[position_key]
        
        # Calculate PnL
        pnl = (price - position['price']) * position['size']
        pnl_percentage = ((price - position['price']) / position['price']) * 100
        
        # Update pair performance tracking
        self.pair_selector.update_pair_performance(symbol, pnl)
        
        # Record the sell trade
        trade = {
            'strategy': strategy_name,
            'symbol': symbol,
            'side': 'sell',
            'price': price,
            'size': position['size'],
            'pnl': pnl,
            'pnl_percentage': pnl_percentage,
            'timestamp': datetime.now(),
            'analysis': analysis,
        }
        
        self.trades.append(trade)
        
        # Record in strategy
        strategy.record_trade(symbol, 'sell', price, position['size'], datetime.now())
        
        # Remove position
        del self.positions[position_key]
        
        self.logger.info(f"SELL signal executed: {symbol} at {price:.2f} (PnL: {pnl:.2f}, {pnl_percentage:.2f}%)")
    
    def get_performance_summary(self) -> Dict[str, Any]:
        """Get performance summary for all strategies."""
        summary = {
            'total_trades': len(self.trades),
            'open_positions': len(self.positions),
            'strategies': {},
            'pair_performance': self.pair_selector.get_pair_performance_summary(),
            'current_pairs': self.pair_selector.get_current_pairs(),
            'timeframe': self.timeframe,
            'execution_interval': self.execution_interval,
        }
        
        # Calculate overall PnL
        total_pnl = 0
        for trade in self.trades:
            if trade['side'] == 'sell':
                total_pnl += trade.get('pnl', 0)
        
        summary['total_pnl'] = total_pnl
        
        # Get individual strategy performance
        for strategy_name, strategy in self.strategies.items():
            metrics = strategy.get_performance_metrics()
            summary['strategies'][strategy_name] = metrics
        
        return summary
    
    def stop(self):
        """Stop the strategy manager."""
        self.is_running = False
        
        # Stop all strategies
        for strategy in self.strategies.values():
            strategy.stop()
        
        self.logger.info("Strategy manager stopped")
    
    def reset(self):
        """Reset the strategy manager."""
        self.positions = {}
        self.trades = []
        
        for strategy in self.strategies.values():
            strategy.reset()
        
        self.logger.info("Strategy manager reset")
    
    def force_pair_rescan(self):
        """Force a rescan of available trading pairs."""
        self.pair_selector.force_rescan()
        self.logger.info("Forced pair rescan completed") 