
import logging
import pandas as pd
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple
import time

from src.strategies.strategy_manager import StrategyManager
from src.backtesting.mock_market_api import MockMarketAPI

class BacktestEngine:
    """
    Event-driven backtesting engine.
    Orchestrates the simulation of strategy execution over historical data.
    """
    
    def __init__(self, config: Dict[str, Any], historical_data: Optional[Dict[str, Dict[str, pd.DataFrame]]] = None):
        """
        Initialize the backtest engine.
        
        Args:
            config: Bot configuration
            historical_data: Dictionary of OHLCV DataFrames {symbol: {timeframe: dataframe}}
        """
        self.config = config
        self.logger = logging.getLogger(__name__)

        # Ensure backtest trade persistence is isolated via table prefix.
        # Backtest results should be written to `backtest_trades` etc. in `data/trades.db`.
        # We NO LONGER switch the database file to backtest_results.db
        
        # Load data from DB if not provided
        if not historical_data:
            self.logger.info("No historical data provided, loading from TradeDatabase...")
            from src.utils.trade_database import TradeDatabase
            self.db = TradeDatabase()
            self.historical_data = self._load_data_from_db()
            self.funding_data = self._load_funding_from_db()
        else:
            self.historical_data = historical_data
            self.funding_data = {} # Assuming no manual funding data for now
        
        # Initialize Mock API
        self.mock_api = MockMarketAPI(config, self.historical_data)
        self.mock_api.set_funding_data(self.funding_data)
        
        # CRITICAL: Ensure clean state (no ghost positions from previous runs in memory)
        self.mock_api.reset_state()
        
        # Initialize Strategy Manager with injected Mock API and Prefixed Tracker
        # IMPORTANT: Initialize PerformanceTracker with 'backtest_' prefix here
        from src.utils.performance_tracker import PerformanceTracker
        self.prefixed_tracker = PerformanceTracker(config, table_prefix="backtest_")
        
        # CRITICAL: Clear prior backtest results BEFORE StrategyManager init
        # StrategyManager -> ExecutionEngine -> load_positions_from_db() loads stale positions
        backtest_cfg = (self.config.get("backtesting") or {})
        if bool(backtest_cfg.get("reset_results_db", True)):
            try:
                self.prefixed_tracker.db.delete_all_trades()
                self.logger.info(f"Cleared existing backtest results in backtest_ tables")
            except Exception as e:
                self.logger.warning(f"Failed to clear backtest results: {e}")
        
        self.strategy_manager = StrategyManager(
            config, 
            market_api=self.mock_api,
            performance_tracker=self.prefixed_tracker
        )
        
        # Ensure portfolio manager also uses it (though StrategyManager might handle this if it passes it down)
        # StrategyManager.portfolio_manager doesn't take tracker in init, it creates its own?
        # Let's check PortfolioManager. If it records trades, it needs the tracker.
        # But StrategyManager.__init__ didn't pass tracker to PortfolioManager.
        # It did: self.portfolio_manager = PortfolioManager(config)
        # We need to manually inject it into portfolio manager if passing to strategy manager isn't enough.
        # StrategyManager uses ExecutionEngine which uses the tracker. 
        # PortfolioManager mostly tracks state. ExecutionEngine records trades.
        # So we should be good if ExecutionEngine gets the right tracker.
        
        # Explicitly set it just in case logic elsewhere uses it directly
        self.strategy_manager.portfolio_manager.performance_tracker = self.prefixed_tracker

        # Phase-1: ensure PerformanceTracker has a sensible initial equity baseline in backtests.
        # BacktestEngine does not call StrategyManager.start(), so we must initialize portfolio + tracker here.
        try:
            self.strategy_manager.portfolio_manager.update_portfolio_info(self.mock_api)
            eq = float((self.mock_api.get_account_balance() or {}).get("total_equity", 0.0) or 0.0)
            if eq > 0:
                self.strategy_manager.performance_tracker.set_initial_equity(eq)
        except Exception as e:
            self.logger.warning(f"Failed to initialize backtest equity baseline: {e}")
        
        # Note: Backtest results are now cleared BEFORE StrategyManager init (above)
        
        # Results
        self.results = {
            'trades': [],
            'performance': {},
            'status': 'idx'
        }

    @staticmethod
    def _sanitize_ohlcv(df: pd.DataFrame) -> Tuple[pd.DataFrame, int]:
        """
        Drop corrupted candles before simulation.

        The market_data table contains stretches of placeholder bars (e.g.
        BERA flat at $0.00072 with zero volume while the real price was
        ~$0.88). One such bar produced a fictional 1200x backtest gain. Two
        rules, both far outside any real crypto move:
        1. Zero-volume bars priced >50x away from the series median
        2. Bars jumping >20x (or <1/20) vs the previous surviving close
        """
        if df.empty:
            return df, 0

        original_len = len(df)

        # Rule 1: scale corruption (flat placeholder runs carry zero volume)
        med = df['close'].median()
        if med and med > 0:
            bad_scale = (df['volume'] <= 0) & ((df['close'] < med / 50) | (df['close'] > med * 50))
            df = df[~bad_scale]

        # Rule 2: isolated absurd jumps vs previous close (applied post rule 1
        # so the recovery bar after a dropped run is not falsely flagged)
        if len(df) > 1:
            ratio = df['close'] / df['close'].shift(1)
            bad_jump = (ratio > 20) | (ratio < 0.05)
            df = df[~bad_jump.fillna(False)]

        return df, original_len - len(df)

    def _load_data_from_db(self) -> Dict[str, Dict[str, pd.DataFrame]]:
        """
        Load all available market data from database per symbol and timeframe.
        Returns: {symbol: {timeframe: DataFrame}}
        """
        data = {}
        total_rows = 0
        total_dropped = 0

        # Timeframes we care about for now
        timeframes = ['5m', '15m', '1h', '4h', '1d']

        # Get all distinct symbols from DB (simplification: assume if 1h exists, others might too)
        symbols = self.db.get_market_data_symbols('1h')

        for symbol in symbols:
            data[symbol] = {}
            for tf in timeframes:
                df = self.db.get_market_data(symbol, tf)
                if not df.empty:
                    df, dropped = self._sanitize_ohlcv(df)
                    if dropped:
                        total_dropped += dropped
                        self.logger.warning(f"Sanitized {symbol} {tf}: dropped {dropped} corrupted candles")
                    if df.empty:
                        continue
                    data[symbol][tf] = df
                    total_rows += len(df)

        self.logger.info(f"Loaded data for {len(data)} symbols across {timeframes} "
                         f"({total_rows} candles, {total_dropped} corrupted dropped)")
        return data

    def _load_funding_from_db(self) -> Dict[str, pd.DataFrame]:
        """
        Load historical funding rates from database.
        Returns: {symbol: DataFrame(index=timestamp, columns=[funding_rate])}
        """
        if not hasattr(self, 'db'):
             return {}
             
        data = {}
        # reused get_market_data_symbols to get list of symbols
        # Ideally we should have get_funding_symbols() but this is close enough for now
        symbols = self.db.get_market_data_symbols('1h') 
        
        total_rows = 0
        for symbol in symbols:
            df = self.db.get_funding_rates(symbol)
            if not df.empty:
                data[symbol] = df
                total_rows += len(df)
        
        self.logger.info(f"Loaded funding rates for {len(data)} symbols ({total_rows} total records)")
        return data
        
    def run(self, start_date: datetime, end_date: datetime, interval_minutes: int = 60):
        """
        Run the backtest simulation.
        
        Args:
            start_date: Start datetime
            end_date: End datetime
            interval_minutes: Step size in minutes
        """
        self.logger.info(f"Starting backtest from {start_date} to {end_date}")
        
        # Set running flag manually since we aren't calling start()
        self.strategy_manager.is_running = True
        
        # Generate simulation timestamps
        timestamps = pd.date_range(start=start_date, end=end_date, freq=f'{interval_minutes}T')
        
        total_steps = len(timestamps)
        
        for i, current_time in enumerate(timestamps):
            # Update Mock API time
            self.mock_api.set_time(current_time)
            
            # Run one cycle of Strategy Manager with SIMULATED time
            try:
                # Convert pandas timestamp to unix float
                unix_time = current_time.timestamp()
                self.strategy_manager.run_trading_cycle(current_time=unix_time)
            except Exception as e:
                self.logger.error(f"Error at step {current_time}: {e}")
                
            # Optional: Progress logging
            if i % 100 == 0:
                print(f"Progress: {i}/{total_steps} ({i/total_steps*100:.1f}%)")
                
        # Clean shutdown and forced cleanup
        self.strategy_manager.is_running = False
        self._teardown()
        self.logger.info("Backtest completed")
        
        return self.generate_report()
        
    def generate_report(self) -> Dict[str, Any]:
        """Generate performance report from the backtest."""
        
        # Use the API's comprehensive balance calculation (includes Spot Assets + Perp Equity)
        balance_info = self.mock_api.get_account_balance()
        total_equity = balance_info['total_equity']
        
        # Breakdown
        spot_balance = self.mock_api.get_spot_balance('USDC') # Cash
        perp_balance = self.mock_api.get_perp_balance()['withdrawable']
        
        # positions from api
        positions = self.mock_api.positions
        trades = self.mock_api.orders

        report = {
            'total_equity': total_equity,
            'spot_balance': spot_balance,
            'perp_balance': perp_balance,
            'positions_open': len(positions),
            'total_orders': len(trades),
            'funding_paid': float(getattr(self.mock_api, 'total_funding_paid', 0.0)),
        }

        # Backtest analysis should read performance from the backtest results DB (`trades` table).
        try:
            db = self.strategy_manager.performance_tracker.db
            stats = db.get_strategy_stats(None)
            dd = db.get_drawdown_stats()
            report["backtest_results_db_path"] = str(db.db_path)
            report["backtest_trades"] = int(stats.get("total_trades", 0) or 0)
            report["backtest_total_pnl"] = float(stats.get("total_pnl", 0) or 0)
            report["backtest_win_rate"] = float(stats.get("win_rate", 0) or 0)
            report["backtest_profit_factor"] = float(stats.get("profit_factor", 0) or 0)
            report["backtest_max_drawdown"] = float(dd.get("max_drawdown", 0) or 0)
            report["backtest_max_drawdown_pct"] = float(dd.get("max_drawdown_pct", 0) or 0)
        except Exception as e:
            self.logger.warning(f"Failed to compute backtest DB-based stats: {e}")
        
        return report

    def _teardown(self):
        """
        Force close all open positions at the end of backtest.
        This ensures 'Mark-to-Market' for final equity and clears the live_positions table.
        """
        self.logger.info("Teardown: Force closing all open positions...")
        engine = self.strategy_manager.execution_engine
        
        # Close Single-Leg Positions
        # Convert keys to list to avoid runtime error during modification
        for symbol in list(engine.positions.keys()):
            pos = engine.positions[symbol]
                
            self.logger.info(f"Closing remaining position: {symbol}")
            # close_position signature: (symbol, reason="manual", timestamp=None)
            # It uses market_api to get current price internally
            engine.close_position(
                symbol=symbol, 
                reason="Backtest End - Force Close"
            )
            
        # Close Multi-Leg Positions
        strategies_map = self.strategy_manager.strategies
        for pid in list(engine.multi_leg_positions.keys()):
            pos = engine.multi_leg_positions[pid]
            if not pos.legs:
                continue
                
            # execution_engine.execute_multi_leg_exit expects a symbol to look up the position
            # We use the first leg's symbol as the primary key
            primary_symbol = pos.legs[0].symbol
            
            self.logger.info(f"Closing remaining multi-leg position: {pid}")
            
            # execute_multi_leg_exit(symbol, signal, strategy_name, strategies_map, timestamp)
            engine.execute_multi_leg_exit(
                symbol=primary_symbol,
                signal={'urgency': 'high', 'reason': 'Backtest End - Force Close'},
                strategy_name=pos.strategy,
                strategies_map=strategies_map
            )
            
        self.logger.info("Teardown complete. All positions closed.")
        
        # Final safety cleanup for any ghosts
        self.strategy_manager.execution_engine.performance_tracker.db.clear_open_positions()
