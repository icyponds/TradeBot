"""
Strategy Manager for coordinating multiple trading strategies.
"""

import logging
import time
from typing import Dict, Any, List
from datetime import datetime

from .moving_average_strategy import MovingAverageStrategy
from .rsi_strategy import RSIStrategy


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
        
        # Initialize strategies
        self.strategies = {}
        self.enabled_strategies = config['strategies']['enabled']
        
        self._initialize_strategies()
        
        # Trading state
        self.is_running = False
        self.positions = {}
        self.trades = []
    
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
                time.sleep(60)  # Wait 1 minute between cycles
                
        except KeyboardInterrupt:
            self.logger.info("Strategy manager stopped by user")
        except Exception as e:
            self.logger.error(f"Error in strategy manager: {e}")
        finally:
            self.stop()
    
    def _execute_trading_cycle(self):
        """Execute one trading cycle for all strategies."""
        self.logger.info("Executing trading cycle...")
        
        # Get market data for all symbols
        for symbol in self.config['trading']['symbols']:
            try:
                market_data = self.market_api.get_market_data(symbol)
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