"""
Strategy manager for orchestrating trading strategies.
"""

import logging
import time
import signal
import sys
import json
import random
from typing import Dict, List, Any, Optional, Tuple
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
from src.utils.change_point import PageHinkley
from src.utils.volatility_gate import VolatilityGate
from src.utils.volatility_scaler import VolatilityScaler
from src.utils.statistics import hurst_exponent
from .strategy_selector import StrategySelector
from .execution_engine import ExecutionEngine
from src.models.trade import Trade, Position, MultiLegPosition, PositionLeg

# Strategy imports - only used when enabled in config
STRATEGY_CLASSES = {
    # Advanced Strategies
    'stat_arb': ('statistical_arbitrage_strategy', 'StatisticalArbitrageStrategy'),
    'funding_rate_arbitrage': ('funding_rate_arbitrage_strategy', 'FundingRateArbitrageStrategy'),
    'ou_mean_reversion': ('ou_mean_reversion_strategy', 'OUMeanReversionStrategy'),
    'volatility_breakout': ('volatility_breakout_strategy', 'VolatilityBreakoutStrategy'),
    'adaptive_grid': ('adaptive_grid_strategy', 'AdaptiveGridStrategy'),
    'sentiment_ml': ('sentiment_ml_strategy', 'SentimentMLStrategy'),
    'liquidation_hunter': ('liquidation_hunter_strategy', 'LiquidationHunterStrategy'),
    'cross_sectional_momentum': ('cross_sectional_momentum_strategy', 'CrossSectionalMomentumStrategy'),
}


class StrategyManager:
    """Manages and orchestrates trading strategies."""
    
    def __init__(self, config: Dict[str, Any], market_api: Any = None, performance_tracker: Any = None):
        """
        Initialize the strategy manager.
        
        Args:
            config: Configuration dictionary
            market_api: Optional injected market API (for testing)
            performance_tracker: Optional injected performance tracker (for backtesting)
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
        self.performance_tracker = performance_tracker if performance_tracker else PerformanceTracker(config, data_dir='data')
        
        # Initialize strategies
        self.strategies = self._initialize_strategies()
        
        # Initialize strategy selector for performance-based selection
        self.strategy_selector = self._initialize_strategy_selector()
        
        # Initialize pair selector
        self.pair_selector = self._initialize_pair_selector()

        # Track last execution per strategy for cooldown/throttle
        self._last_trade_ts_by_strategy = {}
        
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
        
        # Per-strategy position limit (default 5)
        self.max_positions_per_strategy = config['trading'].get('max_positions_per_strategy', 5)

        
        # Trading pairs management
        self.max_pairs_to_trade = config['trading'].get('max_pairs_to_trade', 50)
        
        # Calculate execution interval based on timeframe
        self.execution_interval = self._get_execution_interval()
        
        # Initialize Execution Engine
        self.execution_engine = ExecutionEngine(
            self.config,
            self.market_api,
            self.leverage_manager,
            self.portfolio_manager,
            self.performance_tracker,
            self.pair_selector,
            self.strategy_selector  # Pass strategy selector for live feedback
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
        
        # Set strategy manager reference in execution engine (for stat_arb metadata injection)
        self.execution_engine.strategy_manager = self
        
        # Restore stat_arb z-score state from persisted positions
        self._restore_statarb_state()
        
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

        # Per-asset volatility gating (replaces single PageHinkley)
        self._volatility_gate: Optional[VolatilityGate] = None
        
        # Per-asset volatility scaling for z-score thresholds
        self._volatility_scaler: Optional[VolatilityScaler] = None
        self._volatility_scaler_last_update: float = 0.0
        
        # Legacy single proxy (kept for backward compatibility)
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
        self.last_reconcile_check = 0
        
        # Load sys and importlib for reloading
        import sys
        import importlib
        self._sys = sys
        self._importlib = importlib

    def _restore_statarb_state(self):
        """Restore stat_arb z-score state from persisted multi-leg positions."""
        try:
            if not self.execution_engine.multi_leg_positions:
                return
            
            # Build position data list from loaded positions
            positions_data = []
            for pos_id, pos in self.execution_engine.multi_leg_positions.items():
                if not pos.strategy.startswith('stat_arb'):
                    continue
                
                pos_dict = pos.to_dict() if hasattr(pos, 'to_dict') else {}
                positions_data.append(pos_dict)
            
            if not positions_data:
                return
            
            # Find stat_arb strategies and restore their state
            for strat_name, strat in self.strategies.items():
                if strat_name.startswith('stat_arb') and hasattr(strat, 'restore_active_spreads'):
                    strat.restore_active_spreads(positions_data)
                    
        except Exception as e:
            self.logger.warning(f"Failed to restore stat_arb state: {e}")

    def _check_startup_orphans(self):
        """Check for and close active positions belonging to disabled strategies on startup."""
        self.logger.info("Checking for orphan positions...")
        
        # Single-leg positions
        positions_to_close = []
        for symbol, pos in self.positions.items():
            strat = getattr(pos, 'strategy', None) or pos.get('strategy')
            if strat and strat not in self.strategies:
                positions_to_close.append((symbol, strat))
        
        for symbol, strat in positions_to_close:
            # Check if position exists on exchange
            exchange_pos = self.market_api.get_position(symbol)
            exchange_sz = float((exchange_pos or {}).get('size', 0.0))
            
            if abs(exchange_sz) > 0:
                self.logger.warning(f"Found orphan position {symbol} (strategy: {strat}) on exchange (size={exchange_sz}). Closing...")
                self.close_position(symbol, reason="startup_orphan_cleanup")
            else:
                self.logger.warning(f"Found orphan position {symbol} (strategy: {strat}) LOCAL ONLY. Already closed on exchange. Archiving and removing.")
                
                if symbol in self.positions:
                    pos_obj = self.positions[symbol]
                    
                    # Try to find the closing fill to log accurate history
                    exit_price, exit_time, reason, fee = self._find_closing_fill(symbol, pos_obj.side, pos_obj.entry_time)
                    if exit_price == 0.0:
                         exit_price = pos_obj.entry_price # Fallback
                    
                    # Log the trade
                    self.performance_tracker.record_trade_from_position(
                        symbol=symbol,
                        strategy=pos_obj.strategy,
                        side=pos_obj.side,
                        entry_price=pos_obj.entry_price,
                        exit_price=exit_price,
                        size=pos_obj.size,
                        entry_time=pos_obj.entry_time,
                        exit_time=exit_time,
                        capital_at_risk=pos_obj.capital_at_risk or 0,
                        exit_reason=f"Startup Cleanup ({reason})",
                        stop_loss=pos_obj.stop_loss,
                        take_profit=pos_obj.take_profit,
                        leverage=pos_obj.leverage,
                        fees=fee
                    )
                    
                    # Construct Position ID (same logic as ExecutionEngine.save_positions_to_db)
                    pos_id = f"pos_{pos_obj.strategy}_{symbol}"
                    del self.positions[symbol]
                    
                    # Explicitly delete from DB to prevent resurrection
                    if hasattr(self.execution_engine, 'delete_position_from_db'):
                        self.execution_engine.delete_position_from_db(pos_id)
            
        # Multi-leg positions
        ml_to_close = []
        for pid, pos in self.multi_leg_positions.items():
             if pos.strategy not in self.strategies:
                 ml_to_close.append((pos.primary_symbol, pid, pos.strategy, pos))
        
        for symbol, pid, strat, pos in ml_to_close:
             # Check if legs exist on exchange
             legs_active = False
             
             # Check legs if available on position object
             if hasattr(pos, 'legs'):
                 for leg in pos.legs:
                     leg_symbol = leg.get('symbol')
                     if leg_symbol:
                         exch_pos = self.market_api.get_position(leg_symbol)
                         if float((exch_pos or {}).get('size', 0.0)) != 0:
                             legs_active = True
                             break
             
             if legs_active:
                 self.logger.warning(f"Found orphan multi-leg position {pid} (strategy: {strat}) active on exchange. Closing...")
                 dummy_signal = {'action': 'exit', 'type': 'startup_orphan_cleanup', 'urgency': 'high'}
                 self._handle_multi_leg_signal(symbol, dummy_signal, 0.0, strat, {}, timestamp=datetime.now())
             else:
                 self.logger.warning(f"Found orphan multi-leg position {pid} (strategy: {strat}) LOCAL ONLY. Already closed on exchange. Removing local record.")
                 if pid in self.multi_leg_positions:
                     del self.multi_leg_positions[pid]
                     # Delete from DB
                     if hasattr(self.execution_engine, 'delete_position_from_db'):
                         self.execution_engine.delete_position_from_db(pid)
             
        self.logger.info("Orphan position check complete.")

    def _reconcile_strategies_periodic(self):
        """Periodically check for configuration changes and reconcile strategies."""
        try:
             now = time.time()
             # Check every 60 seconds
             if now - getattr(self, 'last_reconcile_check', 0) < 60:
                 return
             
             self.last_reconcile_check = now
             self.reconcile_strategies()
             
        except Exception as e:
            self.logger.error(f"Error in strategy reconciliation loop: {e}")

    def reconcile_strategies(self):
        """
        Reload configuration and reconcile active strategies.
        - Adds new strategies found in config.
        - Removes strategies no longer in config (and ensures positions are closed).
        """
        try:
            # Reload configuration
            if 'src.config.settings' in self._sys.modules:
                self._importlib.reload(self._sys.modules['src.config.settings'])
                from src.config.settings import load_config
                new_config = load_config()
                
                # Check if strategies config changed
                new_instances_cfg = new_config['strategies'].get('instances', [])
                # Convert to dict lookup for easier comparison: name -> def
                new_strategies_def = {}
                if new_instances_cfg:
                    for sdef in new_instances_cfg:
                        name = sdef.get('name', sdef['type'])
                        new_strategies_def[name] = sdef
                else:
                    # Legacy list support
                    for sname in new_config['strategies']['enabled']:
                        sname = sname.strip()
                        if sname:
                            new_strategies_def[sname] = {"type": sname, "name": sname, "timeframe": None}

                current_strategy_names = set(self.strategies.keys())
                new_strategy_names = set(new_strategies_def.keys())
                
                # Identify changes
                added = new_strategy_names - current_strategy_names
                removed = current_strategy_names - new_strategy_names
                
                if not added and not removed:
                    return # No changes

                self.logger.info(f"Configuration change detected. Added: {added}, Removed: {removed}")
                
                # 1. Handle Removed Strategies
                for name in removed:
                    self.logger.info(f"Removing strategy {name}...")
                    # Close all open positions for this strategy
                    positions_to_close = []
                    for symbol, pos in self.positions.items():
                        s = getattr(pos, 'strategy', None) or pos.get('strategy')
                        if s == name:
                            positions_to_close.append(symbol)
                    
                    for symbol in positions_to_close:
                        self.logger.warning(f"Closing position {symbol} because strategy {name} is removed.")
                        self.close_position(symbol, reason="strategy_removed")
                        
                    # Also check multi-leg
                    ml_to_close = []
                    for pid, pos in self.multi_leg_positions.items():
                        if pos.strategy == name:
                            ml_to_close.append((pos.primary_symbol, pid))
                    
                    for symbol, pid in ml_to_close:
                        self.logger.warning(f"Closing multi-leg position {pid} because strategy {name} is removed.")
                        # Check if we have a close method for multi-leg or trigger via signal
                        # Simulating exit signal
                        dummy_signal = {'action': 'exit', 'type': 'strategy_removed', 'urgency': 'high'}
                        self._handle_multi_leg_signal(symbol, dummy_signal, 0.0, name, {}, timestamp=datetime.now())

                    # Remove from active strategies
                    del self.strategies[name]
                    self.logger.info(f"Strategy {name} removed.")

                # 2. Handle Added Strategies
                # We need to access _initialize_strategies helper or just replicate instantiation logic.
                # Reusing _initialize_strategies is hard because it returns a full dict.
                # We'll instantiate individually.
                
                # We need the factory map
                from src.strategies.strategy_manager import STRATEGY_CLASSES
                
                for name in added:
                    sdef = new_strategies_def[name]
                    stype = sdef['type']
                    stimeframe = sdef.get('timeframe')
                    
                    if stype not in STRATEGY_CLASSES:
                        self.logger.error(f"Unknown strategy type {stype} for {name}")
                        continue
                        
                    module_name, class_name = STRATEGY_CLASSES[stype]
                    try:
                        # Import and force reload the module to pick up code changes
                        module = self._importlib.import_module(f"src.strategies.{module_name}")
                        self._importlib.reload(module)
                        
                        strategy_class = getattr(module, class_name)
                        
                        # Instantiate
                        # Constructor typically takes config
                        # We should use the NEW config
                        instance = strategy_class(new_config)
                        
                        # Set timeframe if applicable
                        if stimeframe:
                            instance.timeframe = stimeframe
                            
                        self.strategies[name] = instance
                        self.logger.info(f"Strategy {name} ({stype}/{stimeframe}) initialized and added.")
                        
                    except Exception as e:
                        self.logger.error(f"Failed to instantiate {name}: {e}")

                # Update self.config to new_config so we don't re-trigger
                self.config = new_config
                
                # Re-init strategy selector? 
                # StrategySelector takes 'strategies_map' in some methods, but it builds its own internal list?
                # Actually StrategySelector logic might need a refresh.
                # Looking at StrategySelector usage in existing code... 
                # It seems it's passed 'self.strategies' in execution calls or initialized once.
                # If StrategySelector keeps internal state, we might need to update it.
                # But typically it calculates weights dynamically or based on DB history.
                
                # Update cooldowns config
                self.strategies_cooldowns = (self.config.get("trading", {}) or {}).get("strategy_cooldowns", {}) or {}
                
        except Exception as e:
            self.logger.error(f"Error reconciling strategies: {e}")
            import traceback
            self.logger.error(traceback.format_exc())

    def sync_positions_with_exchange(self):
        """
        Sync local positions with exchange reality.
        
        Protocols:
        1. Ghost Positions (Local but not Exchange): Delete from DB.
        2. Unrecorded Positions (Exchange but not Local): Immediate Close (Nuclear).
        3. Size Mismatch (Both exist, size differs): Smart Adoption.
           - If Exchange < Local: Record 'phantom' trade for difference -> Update DB.
           - If Exchange > Local: Adopt Exchange Size & Price -> Recalc Risk -> Update DB.
        """
        try:
            # 1. Get real positions from exchange
            exchange_positions = self.market_api.get_positions()
            # Map symbol -> position dict for easy lookup
            exchange_map = {p['symbol']: p for p in exchange_positions if p.get('size', 0) != 0}
            exchange_symbols = set(exchange_map.keys())
            
            # 2. Get local positions from DB (source of truth)
            local_positions = self.performance_tracker.db.get_all_live_position_symbols()
            
            # Debug logging for position sync diagnostics
            if exchange_symbols:
                self.logger.debug(f"[Position Sync] Exchange positions: {sorted(exchange_symbols)}")
            if local_positions:
                self.logger.debug(f"[Position Sync] Local DB positions: {sorted(local_positions)}")
            
            # 3. Handling Unrecorded Positions (Exchange but not Local)
            # ---------------------------------------------------------
            # Gather all symbols used in Multi-Leg positions
            multi_leg_symbols = set()
            for pos in self.multi_leg_positions.values():
                for leg in pos.legs:
                    multi_leg_symbols.add(leg.symbol)
            
            if multi_leg_symbols:
                self.logger.debug(f"[Position Sync] Multi-leg symbols: {sorted(multi_leg_symbols)}")

            for symbol in exchange_symbols:
                if symbol not in local_positions and symbol not in multi_leg_symbols:
                    # Double check it's not a part of a multi-leg strategy not indexed by symbol directly?
                    # For now, strict nuclear option on unknown single-leg symbols.
                    
                    exch_pos = exchange_map[symbol]
                    size = float(exch_pos.get('size', 0))
                    side = exch_pos.get('side', 'neutral').lower()
                    
                    # Use abs(size) to catch both long (positive) and short (negative) positions
                    if abs(size) > 0:
                        self.logger.warning(f"🚨 UNRECORDED position detected: {symbol} {side} size={size}. Initiating immediate closure.")
                        
                        # Execute reduce-only market order to close
                        # Opposite side
                        close_side = 'sell' if side == 'long' else 'buy'
                        
                        # Use absolute size for the close order
                        self.execution_engine.market_api.execute_order(
                            symbol=symbol,
                            side=close_side,
                            size=abs(size),
                            reduce_only=True,
                            urgency='high'
                        )

            # 4. Handling Ghost Positions (Local but not Exchange)
            # ----------------------------------------------------
            ghost_symbols = [s for s in local_positions if s not in exchange_symbols]
            changes_made = False
            
            if ghost_symbols:
                changes_made = True
                self.logger.warning(f"Ghost positions detected: {ghost_symbols}")
            
            for symbol in ghost_symbols:
                pos = self.execution_engine.positions.get(symbol)
                if not pos: continue
                    
                # Try to find the closing fill from recent fills
                exit_price, exit_time, reason, fee = self._find_closing_fill(symbol, pos.side, pos.entry_time)
                if exit_price == 0.0:
                     exit_price = pos.entry_price # Fallback
                
                self.logger.warning(f"Closing ghost position {symbol} at {exit_price} ({reason}) Fee: {fee}")
                
                # Record the trade for PnL tracking
                self.performance_tracker.record_trade_from_position(
                    symbol=symbol,
                    strategy=pos.strategy,
                    side=pos.side,
                    entry_price=pos.entry_price,
                    exit_price=exit_price,
                    size=pos.size,
                    entry_time=pos.entry_time,
                    exit_time=exit_time,
                    capital_at_risk=pos.capital_at_risk or 0,
                    exit_reason=reason,
                    stop_loss=pos.stop_loss,
                    take_profit=pos.take_profit,
                    leverage=pos.leverage,
                    fees=fee
                )
                
                # Remove from local state
                del self.execution_engine.positions[symbol]
                
                # Remove from DB
                try:
                    pos_id = f"pos_{pos.strategy}_{symbol}"
                    self.performance_tracker.db.delete_position(pos_id)
                except Exception as e:
                    self.logger.error(f"Error removing ghost position from DB: {e}")

            # 5. Handling Size Mismatches (Smart Adoption)
            # --------------------------------------------
            common_symbols = [s for s in local_positions if s in exchange_symbols]
            for symbol in common_symbols:
                local_pos = self.execution_engine.positions.get(symbol)
                if not local_pos:
                    # Check if this is a multi-leg position parent (symbol matches a multi-leg position)
                    is_multi_leg = any(symbol in [leg.symbol for leg in pos.legs] 
                                       for pos in self.multi_leg_positions.values())
                    if is_multi_leg:
                        # Multi-leg positions are synced separately
                        continue
                    self.logger.warning(f"Symbol {symbol} in DB but not loaded in memory, skipping sync")
                    continue
                exch_pos = exchange_map[symbol]
                
                local_size = float(local_pos.size)
                exch_size = float(exch_pos.get('size', 0))
                local_side = local_pos.side
                # Exchange uses 'szi' where positive = long, negative = short
                exch_szi = float(exch_pos.get('szi', exch_pos.get('size', 0)))
                exch_side = 'long' if exch_szi > 0 else 'short'
                
                # Check for SIDE mismatch first (critical issue)
                if local_side != exch_side:
                    changes_made = True
                    self.logger.error(f"🚨 SIDE MISMATCH for {symbol}: DB={local_side} vs Exch={exch_side}")
                    
                    # Correct the local position side in memory
                    local_pos.side = exch_side
                    
                    # Persist the corrected side to DB
                    try:
                        self.execution_engine._persist_position(local_pos)
                        self.logger.info(f"Corrected {symbol} side in DB to {exch_side}")
                    except Exception as e:
                        self.logger.error(f"Error persisting corrected side for {symbol}: {e}")
                
                # Tolerance for float comparison
                if abs(local_size - exch_size) > 0.0001:
                    changes_made = True
                    self.logger.warning(f"State Drift Detected for {symbol}: DB={local_size} vs Exch={exch_size}")
                    
                    if exch_size < local_size:
                        # CASE A: External Partial Close (Reduced)
                        diff = local_size - exch_size
                        self.logger.info(f"Adopting external reduction check for {symbol}: -{diff:.4f}")
                        
                        # 1. Record Phantom Trade for the difference
                        # We don't know exact exit price unless we search fills, but using current Price/Entry is safe estimate
                        # Ideally search fills (TODO), for now use entry_price to be PnL neutral or current price?
                        # Use entry_price to avoid fake PnL spikes if unknown.
                        # Actually, better to check current price to reflect mark-to-market reality?
                        # Let's stick to safe defaults: try to find fill, else fallback.
                        exit_price, exit_time, reason, fee = self._find_closing_fill(symbol, local_pos.side, local_pos.entry_time)
                        if exit_price == 0.0:
                            exit_price = local_pos.entry_price
                            reason = "External Partial Close"
                        
                        # Proportion of Fees?
                        # If we found a fill, 'fee' might be total fee. We should probably just log it.
                        
                        self.performance_tracker.record_trade_from_position(
                            symbol=symbol,
                            strategy=local_pos.strategy,
                            side=local_pos.side,
                            entry_price=local_pos.entry_price,
                            exit_price=exit_price,
                            size=diff, # Only the closed amount
                            entry_time=local_pos.entry_time,
                            exit_time=datetime.now(),
                            capital_at_risk=0, # Already counted in main pos
                            exit_reason=reason,
                            fees=0.0 # Hard to attribute
                        )
                        
                        # 2. Update DB Size
                        local_pos.size = abs(exch_size)
                        
                        # 3. Persist updated position atomically
                        self.execution_engine._persist_position(local_pos)
                        
                    else:
                        # CASE B: External Add (Increased)
                        diff = exch_size - local_size
                        self.logger.info(f"Adopting external increase for {symbol}: +{diff:.4f}")
                        
                        # 1. Update DB Size
                        local_pos.size = abs(exch_size)
                        
                        # 2. Update Entry Price (Weighted Avg changed)
                        new_entry = float(exch_pos.get('entry_price', local_pos.entry_price))
                        local_pos.entry_price = new_entry
                        
                        # 3. Recalculate Risk
                        # New Risk = (New Size * New Entry) / Leverage
                        lev = local_pos.leverage or 1.0
                        new_risk = (exch_size * new_entry) / lev
                        local_pos.capital_at_risk = new_risk
                        
                        # 4. Persist updated position atomically
                        self.execution_engine._persist_position(local_pos)

            # 6. Multi-Leg Ghost Detection
            ml_ghosts = []
            
            for pos_id, pos in self.multi_leg_positions.items():
                # Check if ALL legs are missing from exchange
                legs_on_exchange = [leg.symbol for leg in pos.legs if leg.symbol in exchange_symbols]
                
                # If NO legs are on exchange, it's a ghost
                # (A partial fill is tricky, but usually 0 legs means fully closed)
                if not legs_on_exchange:
                    ml_ghosts.append(pos_id)
            
            if ml_ghosts:
                changes_made = True
                self.logger.warning(f"Ghost multi-leg positions detected: {ml_ghosts}")
                for pos_id in ml_ghosts:
                    pos = self.multi_leg_positions.get(pos_id)
                    if not pos: continue
                    
                    self.logger.warning(f"Closing ghost multi-leg position {pos_id} (External Close)")
                    
                    # Calculate combined PnL from all legs by fetching exit fills
                    total_pnl = 0.0
                    total_fees = 0.0
                    exit_time = datetime.now()
                    reason = "External Close"
                    
                    for leg in pos.legs:
                        # Get exit details for each leg from exchange fills
                        exit_price, leg_exit_time, leg_reason, leg_fee = self._find_closing_fill(
                            leg.symbol, leg.side, pos.entry_time
                        )
                        
                        if exit_price == 0.0:
                            exit_price = leg.entry_price  # Fallback
                        
                        # Calculate leg PnL
                        price_diff = exit_price - leg.entry_price
                        if leg.side == 'short':
                            price_diff = -price_diff
                        leg_pnl = price_diff * leg.size
                        
                        total_pnl += leg_pnl
                        total_fees += leg_fee
                        exit_time = leg_exit_time  # Use last leg's exit time
                        if 'liquidation' in leg_reason.lower():
                            reason = "Liquidation"
                    
                    # Record trade to trades table
                    avg_entry = sum(leg.entry_price * leg.size for leg in pos.legs) / sum(leg.size for leg in pos.legs) if pos.legs else 0
                    total_size = sum(leg.size for leg in pos.legs)
                    
                    self.performance_tracker.record_trade_from_position(
                        symbol=pos.primary_symbol,
                        strategy=pos.strategy,
                        side="multi_leg",
                        entry_price=avg_entry,
                        exit_price=avg_entry,  # Not meaningful for multi-leg, use pnl_override
                        size=total_size,
                        entry_time=pos.entry_time,
                        exit_time=exit_time,
                        capital_at_risk=pos.capital_at_risk or 0,
                        exit_reason=reason,
                        pnl_override=total_pnl,
                        fees=total_fees
                    )
                    
                    self.logger.info(f"Recorded ghost multi-leg trade {pos_id}: PnL=${total_pnl:.2f}, Fees=${total_fees:.2f}")
                    
                    # Remove from DB
                    try:
                        if hasattr(self.execution_engine, 'delete_position_from_db'):
                             self.execution_engine.delete_position_from_db(pos_id)
                        else:
                             self.performance_tracker.db.delete_position(pos_id)
                    except Exception as e:
                        self.logger.error(f"Failed to delete ghost ML position {pos_id} from DB: {e}")
                        
                    # Remove from local state
                    del self.execution_engine.multi_leg_positions[pos_id]
                    self.consecutive_errors = 0 # Reset error counter on successful cleanup
                    
            # Note: All position changes are now persisted atomically during their handling.
            # No bulk save needed here.
            
        except Exception as e:
            self.logger.error(f"Error syncing positions with exchange: {e}")

    def _find_closing_fill(self, symbol: str, side: str, entry_time: datetime) -> Tuple[float, datetime, str, float]:
        """
        Search for a fill that closed this position.
        
        Args:
            symbol: Trading pair
            side: 'long' or 'short' (Position side)
            entry_time: Time the position was opened (to filter newer fills)
            
        Returns:
            Tuple: (Exit Price, Exit Time, Reason, Fee)
        """
        exit_price = 0.0
        reason = "Manual / Unknown"
        fill_time_dt = datetime.now()
        fee = 0.0
        
        try:
            # Increase limit to look further back (Fix for ghost position not found if older than 100 trades)
            fills = self.market_api.get_user_fills(limit=1000)
            
            # Look for a fill that closed this position (opposite side, after entry)
            # Handle 'S'/'Sell' vs 'B'/'Buy' normalization
            target_side_norm = 'S' if side == 'long' else 'B'
            
            # Entry timestamp (ms)
            entry_ts = entry_time.timestamp() * 1000 if entry_time else 0
            
            # CLOCK SKEW TOLERANCE: Allow fills up to 60 seconds BEFORE our recorded entry time
            # because local clock might be ahead of exchange clock.
            skew_tolerance_ms = 60 * 1000
            search_start_ts = entry_ts - skew_tolerance_ms
            
            self.logger.info(f"Searching for closing fill for {symbol} {target_side_norm} after {datetime.fromtimestamp(search_start_ts/1000)} (Entry: {entry_time})")
            
            for fill in fills:
                # 1. Symbol check
                if fill.get('coin') != symbol:
                    continue
                    
                # 2. Side check (Normalize fill side)
                fill_side = str(fill.get('side', '')).upper()
                # Handle full words 'SELL', 'BUY' or short 'S', 'B'
                fill_side_norm = 'S' if fill_side.startswith('S') else 'B' if fill_side.startswith('B') else '?'
                
                if fill_side_norm != target_side_norm:
                    continue
                    
                # 3. Time check
                fill_time = fill.get('time', 0)
                if fill_time > search_start_ts:
                    # FOUND A MATCH
                    exit_price = float(fill.get('px', 0.0))
                    fill_time_dt = datetime.fromtimestamp(fill_time / 1000)
                    
                    # Extract Fee
                    # API returns 'fee' in various formats, usually float string or float
                    fee_raw = fill.get('fee', 0.0)
                    try:
                        fee = float(fee_raw)
                    except (ValueError, TypeError):
                        fee = 0.0
                    
                    if 'liquidation' in str(fill.get('dir', '')).lower():
                        reason = "Liquidation"
                    else:
                        reason = "External Close"
                        
                    self.logger.info(f"Found match: {fill_time_dt} @ {exit_price} (Fee: {fee})")
                    return exit_price, fill_time_dt, reason, fee
            
            self.logger.warning(f"No matching closing fill found for ghost {symbol} in last 1000 fills.")
                        
        except Exception as e:
            self.logger.debug(f"Could not fetch fills for ghost reconciliation: {e}")
            
        return exit_price, fill_time_dt, reason, fee

    def _sync_positions_periodic(self):
        """Periodically sync positions with exchange (every 5 minutes)."""
        try:
            now = time.time()
            # Check every 300 seconds (5 minutes)
            if now - getattr(self, 'last_position_sync', 0) < 300:
                return
            
            self.last_position_sync = now
            self.sync_positions_with_exchange()
            
        except Exception as e:
            self.logger.error(f"Error in position sync loop: {e}")
    
    def _refresh_dead_mans_switch_periodic(self):
        """Refresh dead man's switch heartbeat every 15 seconds."""
        try:
            now = time.time()
            # Refresh every 15 seconds
            if now - getattr(self, 'last_heartbeat_refresh', 0) < 15:
                return
            
            self.last_heartbeat_refresh = now
            
            if hasattr(self.market_api, 'refresh_dead_mans_switch'):
                self.market_api.refresh_dead_mans_switch(30)  # 30 second timeout
                
        except Exception as e:
            self.logger.error(f"Error refreshing dead man's switch: {e}")

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

    def get_current_regime(self) -> str:
        """
        Get the current market regime from the HMM allocator.
        
        Returns:
            Current regime: 'range', 'trend', or 'high_vol'
        """
        if self._regime_result:
            return str(self._regime_result.get("regime", "range"))
        return "range"  # Default
    
    def get_volatility_ratio(self, symbol: str) -> float:
        """
        Get the volatility ratio for a symbol (used to scale z-score thresholds).
        
        Args:
            symbol: Trading symbol
            
        Returns:
            Volatility ratio (ATR / median ATR), clamped to [0.8, 1.5], default 1.0
        """
        if self._volatility_scaler:
            return self._volatility_scaler.get_ratio(symbol)
        return 1.0

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
            # Check for per-asset mode vs legacy single-proxy mode
            per_asset_mode = cp.get("per_asset", True)  # Default to per-asset
            
            if per_asset_mode:
                # New: Per-asset volatility gate with correlation blocking
                try:
                    apply_to = set(cp.get("apply_to_strategies", ["ou_mean_reversion", "stat_arb"]) or [])
                    self._volatility_gate = VolatilityGate(
                        correlation_manager=self.correlation_manager,
                        entry_threshold=float(cp.get("threshold", 0.02)),
                        exit_threshold=float(cp.get("exit_threshold", 0.01)),
                        correlation_block_threshold=float(cp.get("correlation_threshold", 0.70)),
                        delta=float(cp.get("delta", 0.0)),
                        alpha=float(cp.get("alpha", 0.99)),
                        apply_to_strategies=apply_to,
                    )
                    self._entry_block_strategies = apply_to
                    self.logger.info(
                        "Volatility gate enabled (per-asset Page-Hinkley): "
                        f"entry_threshold={cp.get('threshold', 0.02)}, "
                        f"exit_threshold={cp.get('exit_threshold', 0.01)}, "
                        f"correlation_threshold={cp.get('correlation_threshold', 0.70)}, "
                        f"strategies={sorted(apply_to)}"
                    )
                except Exception as e:
                    self._volatility_gate = None
                    self.logger.warning(f"Failed to initialize volatility gate; disabled. Error: {e}")
            else:
                # Legacy: single proxy mode (backward compatibility)
                try:
                    self._change_point = PageHinkley(
                        delta=float(cp.get("delta", 0.0)),
                        threshold=float(cp.get("threshold", 0.02)),
                        alpha=float(cp.get("alpha", 0.99)),
                    )
                    self._entry_block_strategies = set(cp.get("apply_to_strategies", []) or [])
                    self.logger.info(
                        "Change-point gate enabled (legacy single-proxy): "
                        f"proxy={cp.get('proxy_symbol','BTC')}/{cp.get('timeframe','15m')}, "
                        f"cooldown={cp.get('cooldown_minutes',20)}m, "
                        f"strategies={sorted(self._entry_block_strategies)}"
                    )
                except Exception as e:
                    self._change_point = None
                    self.logger.warning(f"Failed to initialize change-point detector; disabled. Error: {e}")
        
        # Initialize volatility scaler for regime-adaptive z-score thresholds
        vs = rm.get("volatility_scaler", {}) or {}
        if vs.get("enabled", True):  # Enabled by default when change_point is enabled
            try:
                self._volatility_scaler = VolatilityScaler(
                    lookback=int(vs.get("lookback", 14)),
                    min_multiplier=float(vs.get("min_multiplier", 0.8)),
                    max_multiplier=float(vs.get("max_multiplier", 1.5)),
                )
                self.logger.info(
                    "Volatility scaler enabled: "
                    f"lookback={vs.get('lookback', 14)}, "
                    f"range=[{vs.get('min_multiplier', 0.8)}, {vs.get('max_multiplier', 1.5)}]"
                )
            except Exception as e:
                self._volatility_scaler = None
                self.logger.warning(f"Failed to initialize volatility scaler; disabled. Error: {e}")

    def _is_entry_block_active(self) -> bool:
        """Check if legacy time-based block is active."""
        return self._entry_block_until is not None and datetime.now() < self._entry_block_until

    def _is_strategy_entry_blocked(self, strategy_name: str, symbol: str = None) -> bool:
        """
        Check if a strategy is blocked from entering positions.
        
        Args:
            strategy_name: Name of the strategy
            symbol: Optional symbol to check (for per-asset blocking)
            
        Returns:
            True if blocked, False otherwise
        """
        # Check per-asset volatility gate (new)
        if self._volatility_gate and symbol:
            if self._volatility_gate.is_strategy_blocked(strategy_name, symbol):
                return True
        
        # Check legacy time-based block (backward compatibility)
        if self._is_entry_block_active() and strategy_name in self._entry_block_strategies:
            return True
        
        return False

    def _get_effective_strategy_weight(self, strategy_name: str) -> float:
        """Selector weight multiplied by regime multiplier (if enabled)."""
        base_weight = float(self.strategy_selector.get_strategy_weight(strategy_name))

        # Apply optional regime multiplier
        if self._regime_allocator and self._regime_result:
            regime = str(self._regime_result.get("regime", "range"))
            mult = float(self._regime_allocator.get_multiplier(strategy_name, regime))
            base_weight = base_weight * mult

        # Apply per-strategy caps/floors (fractions of total weight)
        caps_cfg = (
            (self.config.get("risk_management", {}) or {}).get("strategy_weight_caps", {})
            or {}
        )
        caps = caps_cfg.get(strategy_name) or {}
        min_cap = float(caps.get("min", 0.0))
        max_cap = float(caps.get("max", 1.0))

        return max(min_cap, min(max_cap, base_weight))

    def _maybe_update_regime_and_changepoint(self) -> None:
        """Update regime and change-point state."""
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
            # Update regime allocator (always uses proxy)
            if self._regime_allocator and ra.get("enabled", False):
                df = self.market_api.get_ohlcv(proxy_symbol, timeframe, limit=max(lookback, 120))
                if df is not None and len(df) >= 50:
                    X = self._regime_allocator.build_features_from_ohlcv(df)
                    res = self._regime_allocator.update(X, now_ts)
                    self._regime_result = {"regime": res.regime, "probs": res.probs}
                    self.logger.debug(f"Regime={res.regime} probs={res.probs}")

            # Update per-asset volatility gate (new)
            if self._volatility_gate and cp.get("enabled", False):
                self._update_per_asset_volatility_gate(timeframe)
            
            # Update legacy single-proxy change-point detector (backward compatibility)
            elif self._change_point and cp.get("enabled", False):
                df = self.market_api.get_ohlcv(proxy_symbol, timeframe, limit=max(lookback, 120))
                if df is not None and len(df) >= 50:
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
                            self._change_point.reset()
            
            # Update volatility scaler periodically (every 60 seconds)
            if self._volatility_scaler and (now_ts - self._volatility_scaler_last_update) > 60:
                self._update_volatility_scaler(timeframe)
                self._volatility_scaler_last_update = now_ts

        except Exception as e:
            self.logger.debug(f"Regime/changepoint update failed: {e}")
    
    def _update_per_asset_volatility_gate(self, timeframe: str) -> None:
        """Update volatility gate for all ready-to-trade pairs."""
        try:
            ready_pairs = self.pair_selector.pairs_ready_to_trade()
            
            for symbol in ready_pairs:
                df = self.market_api.get_ohlcv(symbol, timeframe, limit=5)
                if df is None or len(df) < 2:
                    continue
                
                close = df["close"].astype(float).values
                if len(close) >= 2 and close[-2] > 0:
                    abs_return = abs((close[-1] - close[-2]) / close[-2])
                    self._volatility_gate.update(symbol, abs_return)
                    
        except Exception as e:
            self.logger.debug(f"Per-asset volatility gate update failed: {e}")
    
    def _update_volatility_scaler(self, timeframe: str) -> None:
        """Update volatility scaler with ATR ratios for all symbols."""
        try:
            ready_pairs = self.pair_selector.pairs_ready_to_trade()
            
            def ohlcv_getter(symbol: str, tf: str, limit: int):
                return self.market_api.get_ohlcv(symbol, tf, limit=limit)
            
            self._volatility_scaler.update(ready_pairs, ohlcv_getter, timeframe)
            
        except Exception as e:
            self.logger.debug(f"Volatility scaler update failed: {e}")
    
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
        """Initialize strategies based on instances configuration."""
        import importlib
        
        # Support for Phase 6 Multi-Instance Architecture
        # 'instances' is now the SINGLE Source of Truth.
        instances_config = self.config['strategies'].get('instances')
        
        if not instances_config:
            self.logger.warning("No strategy instances configured in 'strategies.instances'. Bot will run without active strategies.")
            return {}

        strategy_definitions = instances_config


        strategies = {}
        
        for strategy_def in strategy_definitions:
            strategy_type = strategy_def['type']
            instance_name = strategy_def.get('name', strategy_type)
            timeframe = strategy_def.get('timeframe')

            strategy_type = strategy_type.strip()
            if strategy_type not in STRATEGY_CLASSES:
                self.logger.warning(f"Unknown strategy type: {strategy_type}")
                continue
            
            module_name, class_name = STRATEGY_CLASSES[strategy_type]
            
            try:
                # Dynamically import the strategy module
                module = importlib.import_module(f'.{module_name}', package='src.strategies')
                strategy_class = getattr(module, class_name)
                
                # Instantiate with injected dependencies and TIMEFRAME
                if strategy_type == 'stat_arb':
                    strategies[instance_name] = strategy_class(
                        self.config, self.market_api, self.correlation_manager, timeframe=timeframe
                    )
                elif strategy_type == 'funding_rate_arbitrage':
                    strategies[instance_name] = strategy_class(
                        self.config, self.market_api, timeframe=timeframe
                    )
                else:
                    strategies[instance_name] = strategy_class(self.config, timeframe=timeframe)
                
                self.logger.info(f"Initialized strategy instance: {instance_name} ({strategy_type}) on {strategies[instance_name].timeframe}")
                
            except Exception as e:
                self.logger.error(f"Failed to initialize strategy {instance_name}: {e}")
        
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
    
    def get_required_timeframes(self) -> list:
        """
        Aggregate required timeframes from all active strategies.
        
        Returns:
            List of unique timeframes needed by the bot (only those used by active strategies).
        """
        timeframes = set()  # Start empty, only add what strategies need
        for strategy in self.strategies.values():
            if hasattr(strategy, 'timeframe') and strategy.timeframe:
                timeframes.add(strategy.timeframe)
        # Sort for consistent ordering (5m, 15m, 1h, 4h, 1d)
        tf_order = ['1m', '5m', '15m', '30m', '1h', '4h', '1d']
        return sorted(timeframes, key=lambda x: tf_order.index(x) if x in tf_order else 100)
    
    def _is_data_ready_for_symbol(self, symbol: str) -> bool:
        """
        Check if all required data is available for trading.
        Returns False if ANY condition is not met.
        """
        import time
        required_timeframes = self.get_required_timeframes()
        
        # 1. Check historical data availability for all timeframes
        for tf in required_timeframes:
            ohlcv = self.market_api.get_ohlcv(symbol, tf, limit=20)
            if ohlcv is None or len(ohlcv) < 20:
                self.logger.debug(f"[{symbol}] Insufficient historical data for {tf}")
                return False
        
        # 2. Check cache exists for required timeframes
        for tf in required_timeframes:
            # Retrieve the inner dict for the symbol, then the specific timeframe DF
            symbol_cache = self.market_api.ohlcv_cache.cache.get(symbol)
            if not symbol_cache:
                self.logger.debug(f"[{symbol}] No cache entry for symbol")
                return False
                
            cached_df = symbol_cache.get(tf)
            if cached_df is None or len(cached_df) < 5:
                self.logger.debug(f"[{symbol}] Insufficient cached data for {tf} (found {len(cached_df) if cached_df is not None else 0} rows)")
                return False
        
        # 3. Check WebSocket subscription active
        # If subscribed, data flows through allMids feed which updates all symbols
        if symbol not in self.market_api._subscribed_symbols:
            self.logger.debug(f"[{symbol}] WebSocket not subscribed")
            return False
        
        # 4. Check Global WebSocket connection health
        # Since 'allMids' updates all symbols simultaneously, verifying the
        # global connection is fresh ensures we aren't trading on stale data.
        if hasattr(self.market_api, 'health_monitor') and not self.market_api.health_monitor.is_ws_data_fresh():
            # Track consecutive stale checks for visibility
            if not hasattr(self, '_ws_stale_warn_counter'):
                self._ws_stale_warn_counter = 0
            self._ws_stale_warn_counter += 1
            
            # Log WARNING after 10 consecutive stale checks (visible in logs)
            if self._ws_stale_warn_counter >= 10:
                self.logger.warning(
                    f"[{symbol}] WebSocket stale for {self._ws_stale_warn_counter} consecutive checks - "
                    f"no signals possible until WS recovers"
                )
                self._ws_stale_warn_counter = 0  # Reset to avoid log spam
            else:
                self.logger.debug(f"[{symbol}] WebSocket global connection stale")
            
            # Trigger reconnection attempt if API supports it
            if hasattr(self.market_api, 'attempt_ws_reconnect'):
                self.market_api.attempt_ws_reconnect()
            
            return False
        
        # All checks passed - log first time a symbol becomes ready
        if not hasattr(self, '_symbols_logged_ready'):
            self._symbols_logged_ready = set()
        if symbol not in self._symbols_logged_ready:
            self.logger.info(f"✅ [{symbol}] Data ready for trading")
            self._symbols_logged_ready.add(symbol)
        
        return True
    
    # Force-close thresholds when data is stale (seconds)
    # Shorter timeframes need faster reaction to data issues
    STALE_DATA_FORCE_CLOSE_THRESHOLDS = {
        '5m': 60,      # 1 minute for 5-min strategies
        '15m': 180,    # 3 minutes for 15-min strategies  
        '1h': 600,     # 10 minutes for 1-hour strategies
        '4h': 1800,    # 30 minutes for 4-hour strategies
        '1d': 7200,    # 2 hours for daily strategies
    }
    
    def _get_position_timeframe(self, symbol: str) -> str:
        """Get the primary timeframe of an open position's strategy."""
        position = self.positions.get(symbol)
        if not position:
            return '1h'  # Default fallback
        
        if hasattr(position, 'strategy'):
            strategy_name = position.strategy
        else:
            strategy_name = position.get('strategy', '')
        # Extract timeframe from strategy name suffix (e.g., 'stat_arb_15m' -> '15m')
        # Check longer timeframes first to prevent '15m' from matching '5m'
        for tf in ['15m', '1d', '4h', '1h', '5m']:
            if strategy_name.endswith(tf) or f'_{tf}_' in strategy_name:
                return tf
        return '1h'
    
    def _handle_stale_data_for_symbol(self, symbol: str) -> None:
        """
        Handle stale data scenario for a symbol with an open position.
        
        - If position exists, check if stale duration exceeds timeframe threshold
        - If exceeded, force close the position
        - Otherwise, still attempt exit checks using last known data
        """
        import time
        
        position = self.positions.get(symbol)
        if not position:
            # No position, nothing to manage
            return
        
        # Get the position's timeframe and corresponding threshold
        timeframe = self._get_position_timeframe(symbol)
        force_close_threshold = self.STALE_DATA_FORCE_CLOSE_THRESHOLDS.get(timeframe, 600)
        
        # Calculate how long data has been stale for this symbol
        last_tick_time = self.market_api._symbol_last_tick.get(symbol)
        if last_tick_time is None:
            stale_duration = float('inf')
        else:
            stale_duration = time.time() - last_tick_time
        
        self.logger.warning(
            f"[{symbol}] Data stale for {stale_duration:.1f}s (threshold: {force_close_threshold}s for {timeframe})"
        )
        
        # Check if we should force close
        if stale_duration >= force_close_threshold:
            self.logger.error(
                f"[{symbol}] Force closing position - data stale for {stale_duration:.1f}s "
                f"exceeds {timeframe} threshold of {force_close_threshold}s"
            )
            self.execution_engine.close_position(symbol, reason=f"stale_data_{timeframe}_timeout")
            return
        
        # Data is stale but not yet at force-close threshold
        # Try to run exit logic using last known price (if available)
        try:
            # Get last known price from cache
            price_data = self.market_api._price_data.get(symbol)
            if price_data and len(price_data) > 0:
                last_price = price_data[-1].get('price')
                if last_price:
                    self.logger.info(f"[{symbol}] Running exit check with last known price: {last_price}")
                    # Check stop loss / take profit with existing position data
                    self._check_exit_conditions_with_price(symbol, position, last_price)
        except Exception as e:
            self.logger.error(f"[{symbol}] Error running stale-data exit check: {e}")
    
    def _check_exit_conditions_with_price(self, symbol: str, position: dict, current_price: float) -> None:
        """Check basic exit conditions (stop loss, take profit) using provided price."""
        if hasattr(position, 'entry_price'):
            # Object access
            entry_price = position.entry_price or 0
            stop_loss = position.stop_loss
            take_profit = position.take_profit
            side = position.side or 'long'
        else:
            # Dict access
            entry_price = position.get('entry_price', 0)
            stop_loss = position.get('stop_loss')
            take_profit = position.get('take_profit')
            side = position.get('side', 'long')
        
        if not entry_price:
            return
        
        # Check stop loss
        if stop_loss:
            if (side == 'long' and current_price <= stop_loss) or \
               (side == 'short' and current_price >= stop_loss):
                self.logger.warning(f"[{symbol}] Stop loss triggered during stale data: {current_price}")
                self.execution_engine.close_position(symbol, reason="stop_loss_stale_data")
                return
        
        # Check take profit
        if take_profit:
            if (side == 'long' and current_price >= take_profit) or \
               (side == 'short' and current_price <= take_profit):
                self.logger.info(f"[{symbol}] Take profit triggered during stale data: {current_price}")
                self.execution_engine.close_position(symbol, reason="take_profit_stale_data")
                return
    
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
        
        # Check for orphan positions (strategies no longer enabled)
        self._check_startup_orphans()
        
        # SAFETY: Cancel all open orders before sync (clean slate on restart)
        if hasattr(self.market_api, 'cancel_all_orders'):
            try:
                cancelled = self.market_api.cancel_all_orders()
                if cancelled > 0:
                    self.logger.warning(f"🧹 Cancelled {cancelled} stale orders on startup")
            except Exception as e:
                self.logger.error(f"Failed to cancel orders on startup: {e}")
        
        # SAFETY: Enable dead man's switch (auto-cancel if bot crashes)
        if hasattr(self.market_api, 'set_dead_mans_switch'):
            try:
                self.market_api.set_dead_mans_switch(30)  # 30 second timeout
            except Exception as e:
                self.logger.error(f"Failed to set dead man's switch: {e}")
        
        # Sync with exchange to clear any ghost positions (closed while bot was off)
        self.sync_positions_with_exchange()
        
        # Check if any loaded positions already meet TP/SL conditions
        self.check_startup_exits()
        
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
        
        # SAFETY: Disable dead man's switch on graceful shutdown
        if hasattr(self.market_api, 'disable_dead_mans_switch'):
            try:
                self.market_api.disable_dead_mans_switch()
            except Exception as e:
                self.logger.error(f"Failed to disable dead man's switch: {e}")
        
        # Stop market API (only if it has a stop method)
        if hasattr(self.market_api, 'stop'):
            self.market_api.stop()
        
        self.logger.info("Strategy manager stopped")
    
    def emergency_stop(self):
        """Emergency stop - close all positions and stop trading."""
        self.logger.warning("EMERGENCY STOP: Closing all positions...")
        self.stop(close_positions=True)

    def close_all_positions(self, reason: str = "emergency_cleanup") -> None:
        """
        Close all currently open positions.
        
        Args:
            reason: Reason for closure
        """
        self.logger.info(f"Closing all positions (Reason: {reason})...")
        
        # Get list of symbols to avoid dictionary size change checks
        # Close multi-leg positions first
        multi_leg_ids = list(self.multi_leg_positions.keys())
        if multi_leg_ids:
             self.logger.info(f"Closing {len(multi_leg_ids)} multi-leg positions...")
             for pos_id in multi_leg_ids:
                 try:
                     self.logger.info(f"Closing multi-leg position: {pos_id}")
                     if hasattr(self, 'execution_engine'):
                         # Assuming close_multi_leg_position exists - user confirmed this logic
                         # If it doesn't exist, we'll catch the attribute error
                         if hasattr(self.execution_engine, 'close_multi_leg_position'):
                            self.execution_engine.close_multi_leg_position(pos_id, reason=reason)
                         else:
                             # Fallback: close legs individually if method missing
                             self.logger.warning("close_multi_leg_position not found, closing legs individually")
                             pos = self.multi_leg_positions.get(pos_id)
                             if pos:
                                 for leg in pos.legs:
                                     self.execution_engine.close_position(leg.symbol, reason=reason)
                 except Exception as e:
                     self.logger.error(f"Error closing multi-leg position {pos_id}: {e}")

        # Get list of symbols to avoid dictionary size change checks
        symbols = list(self.positions.keys())
        
        if not symbols and not multi_leg_ids:
            self.logger.info("No open positions to close.")
            return

        for symbol in symbols:
            try:
                self.logger.info(f"Closing position: {symbol}")
                # Use execution engine to close
                if hasattr(self, 'execution_engine'):
                    self.execution_engine.close_position(symbol, reason=reason)
                else:
                    self.logger.error("Execution engine not initialized, cannot close position")
            except Exception as e:
                self.logger.error(f"Error closing position {symbol}: {e}")
    
    # NOTE: sync_positions_with_exchange is now defined earlier in the class (around line 337)
    # with improved PnL tracking for ghost positions.

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

    def run_trading_cycle(self, current_time: float = None) -> bool:
        """
        Run a single iteration of the trading logic.
        
        Args:
            current_time: Optional timestamp (unix seconds) for backtesting. 
                         If None, uses time.time().
        
        Returns:
            bool: True if trading logic ran (pairs analyzed), False if skipped (no pairs)
        """
        try:
            if current_time is None:
                current_time = time.time()
                current_datetime = datetime.now()
            else:
                current_datetime = datetime.fromtimestamp(current_time)

            # Update regime and change-point gating from market proxy (once per cycle)
            self._maybe_update_regime_and_changepoint()
            
            # NOTE: Pair rotation is now handled by the continuous scouting loop
            # in DynamicPairSelector._background_data_fetcher(). No rescan trigger needed.
            
            # Retry any pending symbol subscriptions from previous failures
            if hasattr(self.market_api, 'retry_pending_subscriptions'):
                self.market_api.retry_pending_subscriptions()
            
            # With WebSocket, positions are updated in real-time
            # Only sync periodically to ensure accuracy
            if current_time - self.last_position_sync >= self.position_sync_interval:
                self.logger.debug(f"Syncing positions with exchange (local: {len(self.positions)})")
                self.sync_positions_with_exchange()
                self.last_position_sync = current_time
            
            # Check liquidation risks for multi-leg positions
            self._check_liquidation_risks()
            
            # CRITICAL: Ensure ALL open position symbols are subscribed for price updates
            # This prevents positions from being silently skipped due to missing price data
            for symbol in list(self.positions.keys()):
                if symbol not in self.market_api._subscribed_symbols:
                    self.logger.info(f"🔌 Force-subscribing open position: {symbol}")
                    try:
                        self.market_api.subscribe_symbol(symbol)
                    except Exception as e:
                        self.logger.warning(f"Failed to subscribe {symbol}: {e}")
            
            # Update position prices immediately to ensure latest data for monitoring
            self.update_position_prices()

            # Continuous position monitoring and auto-closure
            position_monitoring_interval = self.config['trading']['position_monitoring_interval']
            emergency_portfolio_loss_pct = self.config.get('risk_management', {}).get('emergency_portfolio_loss_pct', 10.0)
            
            if current_time - self.last_position_monitoring >= position_monitoring_interval:
                self.logger.debug(f"Running position monitoring check ({len(self.positions)} positions)")
                self._monitor_and_close_positions(
                    emergency_portfolio_loss_pct, 
                    timestamp=current_datetime
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
            if self.correlation_manager.should_update(current_time=current_datetime):
                # Get all potential symbols from pair selector or config
                all_symbols = self.pair_selector.get_ready_pairs()
                if all_symbols:
                    self.correlation_manager.update_correlations(all_symbols, current_time=current_datetime)
            
            # Get current trading pairs that are fully loaded
            trading_pairs = self.pair_selector.get_ready_pairs()
            
            if not trading_pairs:
                # This is normal during startup while background fetcher loads data
                self.logger.warning("No ready trading pairs available (waiting for data loading)")
                return False
            
            self.logger.info(f"Analyzing {len(trading_pairs)} ready trading pairs")
            
            # Prioritize symbols with open positions to ensure they get data
            # before rate limits are exhausted during timeframe boundaries
            position_symbols = set(self.positions.keys())
            priority_symbols = [s for s in trading_pairs if s in position_symbols]
            other_symbols = [s for s in trading_pairs if s not in position_symbols]
            
            if priority_symbols:
                self.logger.debug(f"Prioritizing {len(priority_symbols)} symbols with open positions: {priority_symbols}")
            
            # Process priority symbols first (positions need timely data for exits)
            for symbol in priority_symbols:
                if not self.is_running:
                    break
                self._analyze_symbol(symbol, timestamp=current_datetime)
            
            # Then process other symbols
            for symbol in other_symbols:
                if not self.is_running:
                    break
                self._analyze_symbol(symbol, timestamp=current_datetime)

            

            
            
            # Display PnL (prices already updated at start of cycle)
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
        
        # Initial pair selection scan - populates backfill queue and starts background fetcher
        self.logger.info("Triggering initial pair selection scan...")
        self.pair_selector.scan_and_select_pairs()
        
        while self.is_running:
            try:
                # Run one cycle of trading logic
                # This has been extracted to allow step-by-step execution in backtesting
                self.run_trading_cycle()

                # Live Reconciliation: Check for config changes every 60s
                self._reconcile_strategies_periodic()
            
                # Update account balance periodically (every 5 minutes)
                self._update_account_balance_periodic()
            
                # Sync positions with exchange every 5 minutes
                self._sync_positions_periodic()
                
                # Refresh dead man's switch heartbeat every ~15 seconds
                self._refresh_dead_mans_switch_periodic()
                
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
        """Subscribe to real-time data for a symbol (uses API's tracking set)."""
        # Use market_api's _subscribed_symbols to avoid duplicate subscriptions
        # pair_selector may have already subscribed during initialization
        if symbol not in self.market_api._subscribed_symbols:
            req_timeframes = self.get_required_timeframes()
            self.market_api.subscribe_symbol(symbol, required_timeframes=req_timeframes)
            self.logger.info(f"Subscribed to real-time data for {symbol}")
    
    
    def _check_triggers_realtime(self, symbol: str, price: float):
        """
        Check for SL/TP triggers immediately on price update.
        Called from WebSocket thread - must be fast and safe.
        """
        position = self.positions.get(symbol)
        if not position:
            return

        exit_reason = None
        
        # Check Stop Loss
        if position.stop_loss:
            if (position.side == 'long' and price <= position.stop_loss) or \
               (position.side == 'short' and price >= position.stop_loss):
                exit_reason = "stop_loss_realtime"
        
        # Check Take Profit
        if not exit_reason and position.take_profit:
            if (position.side == 'long' and price >= position.take_profit) or \
               (position.side == 'short' and price <= position.take_profit):
                exit_reason = "take_profit_realtime"
                
        # Execute if triggered
        if exit_reason:
            self.logger.info(f"⚡ Real-time trigger for {symbol}: {exit_reason} (Price: {price})")
            # Close position immediately
            # Note: close_position is thread-safe enough (uses API lock + dict ops)
            self.execution_engine.close_position(
                symbol=symbol, 
                reason=exit_reason,
                urgency='high' # Use aggressive closing for triggered exits
            )

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
                self.logger.info(f"Real-time price update for {symbol}: ${float(old_price or 0):.4f} → ${float(price or 0):.4f} ({float(price_change or 0):+.2f}%)")
        
        # Real-time Trigger Check
        # Immediately check for SL/TP execution without waiting for polling loop
        if symbol in self.positions:
            self._check_triggers_realtime(symbol, price)

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
                    pos = self.positions[symbol]
                    del self.positions[symbol]
                    # Delete from DB atomically
                    pos_id = f"pos_{pos.strategy}_{symbol}"
                    self.performance_tracker.db.delete_position(pos_id)
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
                    # Persist new position atomically
                    self.execution_engine._persist_position(position)
                else:
                    # Existing position updated
                    local_position = self.positions[symbol]
                    old_size = local_position.size
                    if abs(position_size - old_size) > 0.001:  # Significant size change
                        self.logger.info(f"Real-time position update: {symbol} size changed {old_size} → {position_size}")
                        local_position.size = position_size
                        # Persist updated position atomically
                        self.execution_engine._persist_position(local_position)
            
        except Exception as e:
            self.logger.error(f"Error handling position update: {e}")
    
    def _analyze_symbol(self, symbol: str, timestamp: datetime = None):
        """Analyze a single symbol and execute strategies with per-strategy timeframes."""
        try:
            # Subscribe to real-time data for this symbol
            self._subscribe_to_symbol(symbol)
            
            # NEW: Check data readiness before any analysis
            if not self._is_data_ready_for_symbol(symbol):
                # Handle positions with stale data (exit checks, force close if needed)
                self._handle_stale_data_for_symbol(symbol)
                self.logger.debug(f"[{symbol}] Data not ready, skipping new analysis")
                return
            
            # Check if we have sufficient data (legacy check, may overlap with readiness)
            if not self.market_api.is_data_available(symbol):
                self.logger.debug(f"Insufficient data for {symbol}, skipping")
                return
            
            # Get current price
            market_data = self.market_api.get_market_data(symbol)
            if not market_data:
                self.logger.warning(f"Could not get market data for {symbol}")
                return
            
            current_price = market_data['current_price']
            
            # Fetch data for all timeframes required by active strategies
            ohlcv_dict = {}
            # Use dynamic timeframes instead of hardcoded list
            target_timeframes = self.get_required_timeframes()
            
            has_sufficient_data = False
            for tf in target_timeframes:
                # Use a reasonable limit
                df = self.market_api.get_ohlcv(symbol, tf, self.ohlcv_limit)
                if df is not None and len(df) >= 20:
                    ohlcv_dict[tf] = df
                    has_sufficient_data = True
            
            if not has_sufficient_data:
                self.logger.debug(f"Insufficient OHLCV data for {symbol} (checked {target_timeframes})")
                return

            # Collect all signals from all strategies for this symbol
            collected_signals = []
            
            for strategy_name, strategy in self.strategies.items():
                strategy_class_name = strategy.__class__.__name__
            
                # Handle special strategies
                if strategy_class_name == 'FundingRateArbitrageStrategy':
                    # Funding rate arb is multi-leg and doesn't conflict with single-leg strategies
                    # It uses funding rates, but we pass full context
                    self._execute_strategy(symbol, strategy_name, strategy, ohlcv_dict, current_price)
                    continue
                
                if strategy_class_name == 'MomentumFactorStrategy':
                    # Momentum is handled separately in _run_portfolio_strategies
                    continue
                
                # Check if strategy's preferred timeframe is available
                # If not, we might skip or let strategy handle fallback
                # BaseStrategy logic usually handles fallback or returns None
                
                # Generate signal without executing
                signal = self._generate_signal_for_strategy(symbol, strategy_name, strategy, ohlcv_dict, current_price)
                if signal:
                    self.logger.info(f"🎯 Signal generated: {strategy_name}/{symbol} → {signal.get('signal', signal.get('action', 'unknown'))}")
                    collected_signals.append({
                        'strategy_name': strategy_name,
                        'strategy': strategy,
                        'signal': signal,
                        'ohlcv': ohlcv_dict,
                        'current_price': current_price
                    })
            
            # Resolve conflicts and execute the winning signal
            if collected_signals:
                winning_signal = self._resolve_signal_conflicts(symbol, collected_signals)
            if collected_signals:
                winning_signal = self._resolve_signal_conflicts(symbol, collected_signals)
                if winning_signal:
                    self._execute_resolved_signal(symbol, winning_signal, timestamp=timestamp)
            
            # Update metadata for existing multi-leg positions involving this symbol
            # (e.g. Sync Z-Scores from Strategy Memory to Database for Dashboard)
            ml_pos = self._get_multi_leg_position_for_symbol(symbol)
            if ml_pos and ml_pos.strategy == 'stat_arb':
                strategy = self.strategies.get('stat_arb')
                if strategy and hasattr(strategy, 'get_spread_status'):
                    status = strategy.get_spread_status(symbol)
                    if status:
                        # Only update if changed significantly to save DB writes
                        # Or just update memory and let periodic save handle DB?
                        # For now, update memory. Dashboard reads from memory (self.multi_leg_positions)
                        ml_pos.metadata.update(status)
                
        except Exception as e:
            self.logger.error(f"Error analyzing {symbol}: {e}")
    
    def _generate_signal_for_strategy(self, symbol: str, strategy_name: str, strategy, ohlcv: Dict[str, pd.DataFrame], current_price: float) -> Optional[Dict[str, Any]]:
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
                # MTF strategies now accept symbol and ohlcv dict
                signal = strategy.generate_signal(symbol, ohlcv)
            
            if signal:
                # Dynamic Efficiency Filter (Hurst Exponent)
                # If market is trending/efficient (Hurst > 0.6), Mean Reversion is dangerous.
                # Require stricter 3.0 sigma threshold (instead of 2.0) for entry.
                if signal.get("action") == "open":
                    is_mean_reversion = strategy_name.startswith('ou_mean_reversion') or strategy_name.startswith('stat_arb')
                    
                    if is_mean_reversion:
                        z = signal.get("zscore")
                        if z is None:
                            z = signal.get("z_score")
                        
                        if z is not None:
                            # Calculate Hurst for the symbol to gauge efficiency
                            # Use sufficient lookback (e.g., 100 periods)
                            try:
                                ohlcv_df = ohlcv.get('15m')
                                if ohlcv_df is None:
                                    ohlcv_df = ohlcv.get('1h')
                                if ohlcv_df is None and len(ohlcv) > 0:
                                    ohlcv_df = list(ohlcv.values())[0]
                                    
                                if ohlcv_df is not None and len(ohlcv_df) > 50:
                                    # Quick Hurst calculation on recent 100 candles
                                    h_value = hurst_exponent(ohlcv_df['close'].iloc[-100:])
                                    
                                    # Threshold: 0.6 indicates persistent trend / efficiency
                                    if h_value > 0.6:
                                        if abs(z) < 3.0:
                                            self.logger.info(f"Rejected signal for {strategy_name}/{symbol}: Efficiency Filter (H={h_value:.2f}), Z-Score {z:.2f} < 3.0")
                                            return None
                            except Exception as e:
                                # Don't block on calculation error, fall through
                                pass

                # Phase-1: Strategy-Specific Entry Hurdle
                # Only valid if the strategy explicitly configures 'zscore_hurdle_buffer'.
                # Strict check: No global fallback.
                try:
                    if signal.get("action") == "open":
                        # Resolve config
                        strat_type = getattr(strategy, 'strategy_name', strategy_name).replace('_15m','').replace('_1h','').replace('_4h','')
                        strat_specific_cfg = self.config.get('strategies', {}).get(strat_type, {})
                        
                        # Check strictly for the existence of the key
                        if "zscore_hurdle_buffer" in strat_specific_cfg:
                            buf = float(strat_specific_cfg["zscore_hurdle_buffer"])
                            
                            # Get Z and Threshold
                            z = signal.get("zscore")
                            if z is None:
                                z = signal.get("z_score")
                            
                            entry_thr = getattr(strategy, "zscore_entry", None)
                            if entry_thr is None:
                                entry_thr = getattr(strategy, "z_score_entry", None)
                                
                            if z is not None and entry_thr is not None:
                                # Logic: |Z| must be >= (EntryThr + Buffer) to pass
                                # e.g. Thr=1.5, Buf=0.3 -> Need |Z| >= 1.8
                                if abs(float(z)) < (float(entry_thr) + buf):
                                    self.logger.info(
                                        f"Entry hurdle: skipping {strategy_name}/{symbol} "
                                        f"(|z|={float(z):.2f} < {float(entry_thr):.2f}+{buf:.2f})"
                                    )
                                    return None
                except Exception as e:
                    self.logger.debug(f"Entry hurdle evaluation failed for {strategy_name}/{symbol}: {e}")

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
        # Verify signal integrity first
        buy_signals = []
        sell_signals = []
        
        for s in collected_signals:
            sig_data = s.get('signal')
            if not isinstance(sig_data, dict):
                self.logger.error(f"Invalid signal type for {symbol} from {s.get('strategy_name')}: {type(sig_data)} (expected dict)")
                continue
                
            direction = sig_data.get('signal')
            if direction == 'buy':
                buy_signals.append(s)
            elif direction == 'sell':
                sell_signals.append(s)
        
        # Check for conflict (both buy and sell signals exist)
        if buy_signals and sell_signals:
            # Get best signal from each direction
            best_buy = max(buy_signals, key=lambda s: s['signal'].get('_weighted_strength', 0))
            best_sell = max(sell_signals, key=lambda s: s['signal'].get('_weighted_strength', 0))
            
            buy_strength = best_buy['signal'].get('_weighted_strength', 0)
            sell_strength = best_sell['signal'].get('_weighted_strength', 0)
            
            self.logger.warning(
                f"⚠️ Signal conflict for {symbol}: "
                f"BUY ({best_buy['strategy_name']}, strength={float(buy_strength or 0):.3f}) vs "
                f"SELL ({best_sell['strategy_name']}, strength={float(sell_strength or 0):.3f})"
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
    
    def _execute_resolved_signal(self, symbol: str, signal_data: Dict, timestamp: datetime = None):
        """Execute a resolved signal after conflict resolution."""
        strategy_name = signal_data['strategy_name']
        strategy = signal_data['strategy']
        signal = signal_data['signal']
        ohlcv = signal_data['ohlcv']
        current_price = signal_data['current_price']
        
        self.logger.info(f"{strategy_name} signal for {symbol}: {signal['signal']} at {current_price}")
        
        # Check if this is a multi-leg signal
        if signal.get('signal_type') == 'multi_leg':
            self._handle_multi_leg_signal(symbol, signal, current_price, strategy_name, ohlcv, timestamp=timestamp)
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
            
            # Safety Clamp: Ensure final position value meets exchange minimum ($10)
            # This handles cases where exploration scaling reduced the size below requirements
            # or if any other adjustment shrank it
            try:
                current_size = float(signal.get("size", 0))
                current_value = current_size * current_price
                MIN_ORDER_VALUE = 12.0
                
                if current_value > 0 and current_value < MIN_ORDER_VALUE:
                    original_val = current_value
                    signal["size"] = MIN_ORDER_VALUE / current_price
                    
                    # Update margin proportional to the size increase to maintain leverage
                    if "margin_required" in signal:
                        leverage = float(signal.get("leverage", 1.0))
                        # Safety check for zero leverage
                        if leverage <= 0: leverage = 1.0
                        signal["margin_required"] = MIN_ORDER_VALUE / leverage
                        
                    self.logger.info(
                        f"Safety clamp: Re-bumped trade for {symbol} from ${original_val:.2f} to ${MIN_ORDER_VALUE:.2f} "
                        f"(after scaling)"
                    )
            except Exception as e:
                self.logger.error(f"Error in safety clamp for {symbol}: {e}")

            # Apply strategy weight to the signal strength for position sizing
            if 'signal_strength' in signal:
                signal['signal_strength'] *= strategy_weight
            
            self.logger.info(f"Executing {strategy_name} trade for {symbol} (weight: {float(strategy_weight or 0):.2f})")
            self._execute_trade(symbol, signal, current_price, strategy_name, ohlcv, timestamp=timestamp)
            self._last_trade_ts_by_strategy[strategy_name] = timestamp if timestamp else datetime.now()
        else:
            self.logger.info(f"Skipping {strategy_name} signal for {symbol} - conditions not met")
    

    
    def _execute_strategy(self, symbol: str, strategy_name: str, strategy, ohlcv: Dict[str, pd.DataFrame], current_price: float):
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
            strategy_class = strategy.__class__.__name__
            
            if strategy_class == 'StatisticalArbitrageStrategy':
                # Stat Arb needs special handling to fetch correlated pair data
                signal = strategy.generate_signal_with_symbol(symbol, ohlcv)
            elif strategy_class == 'FundingRateArbitrageStrategy':
                # Funding Rate Arbitrage needs funding rate data and multi-leg position context
                signal = self._generate_funding_arb_signal(symbol, strategy)
            elif strategy_class == 'OUMeanReversionStrategy':
                # OU Mean Reversion needs symbol context for parameter caching
                signal = strategy.generate_signal_for_symbol(symbol, ohlcv)
            else:
                # Get preferred timeframe data
                tf = getattr(strategy, 'timeframe', '1h')
                # Extract specific dataframe from dict
                strategy_ohlcv = ohlcv.get(tf)
                if strategy_ohlcv is not None:
                    signal = strategy.generate_signal(strategy_ohlcv)
                else:
                     return
            
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
            self.logger.debug(f"Strategy {strategy_name} weight: {float(strategy_weight or 0):.2f}")
            
            # Check if we should act on the signal
            should_execute = self._should_execute_signal(symbol, signal, current_price, ohlcv, strategy_name)
            self.logger.info(f"Should execute {strategy_name} signal for {symbol}: {should_execute}")
            
            if should_execute:
                # Apply strategy weight to the signal strength for position sizing
                if 'signal_strength' in signal:
                    signal['signal_strength'] *= strategy_weight
                
                self.logger.info(f"Executing {strategy_name} trade for {symbol} (weight: {float(strategy_weight or 0):.2f})")
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
                                strategy_name: str, ohlcv: Dict[str, pd.DataFrame], timestamp: datetime = None):
        """Handle multi-leg trade signal."""
        # For exit signals, calculate_signal_strength is not used, so we can provide a fallback
        # This handles cases where strategy_name is an instance name (e.g. "stat_arb_15m") 
        # that may not exist as a key in self.strategies
        action = signal.get('action', '')
        
        if action in ('exit', 'close'):
            # Exit signals don't need signal strength calculation
            signal_strength_fn = lambda *args, **kwargs: 0.5  # Dummy value, not used for exits
        else:
            # Entry signals need actual strategy lookup
            # Try direct lookup first, then try base strategy name
            strategy = self.strategies.get(strategy_name)
            if not strategy:
                # Try extracting base name (e.g., "stat_arb_15m" -> "stat_arb")
                base_name = strategy_name.rsplit('_', 1)[0] if '_' in strategy_name else strategy_name
                strategy = self.strategies.get(base_name)
            
            if not strategy:
                self.logger.error(f"Strategy '{strategy_name}' not found for multi-leg entry")
                return
                
            signal_strength_fn = strategy.calculate_signal_strength
        
        self.execution_engine.handle_multi_leg_signal(
            symbol, signal, current_price, strategy_name, ohlcv, 
            signal_strength_fn,
            self.strategies,
            timestamp=timestamp,
            strategy_manager=self
        )
    
    def _get_leg_price(self, symbol: str, market_type: str) -> Optional[float]:
        """Delegate leg price fetching to execution engine."""
        return self.execution_engine.get_leg_price(symbol, market_type)

    def _check_liquidation_risks(self):
        """Delegate liquidation risk check to execution engine."""
        self.execution_engine.check_liquidation_risks(self.strategies)

    def _check_nuclear_displacement(self, symbol: str, new_strength: float) -> str:
        """
        Check for 'Nuclear Switch' displacement (Multi-Leg vs Single-Leg).
        
        Returns:
            'displacement_arb' if Arb should be displaced.
            'displacement_single' if Single-Leg should be displaced.
            None if no displacement.
        """
        # Case 1: Arb Existing -> New Single-Leg Signal
        # We need to find if this symbol is part of an active multi-leg position
        # Use efficient lookup from Execution Engine
        arb_position = self.execution_engine.get_multi_leg_position_by_leg_symbol(symbol)
        
        if arb_position:
            # Arb Strength is usually stored or needs to be queried. 
            # For now, we assume stored in metadata or approx 0.5 if unknown.
            arb_strength = getattr(arb_position, 'entry_signal_strength', 0.5) or 0.5
            
            # Threshold: Single > Arb * 2.0
            if new_strength > (arb_strength * 2.0):
                self.logger.info(f"☢️ NUCLEAR DISPLACEMENT: New Single ({float(new_strength or 0):.2f}) > Arb ({float(arb_strength or 0):.2f} * 2.0). Closing Arb.")
                return 'displacement_arb'
            else:
                return 'block' # Arb protects itself
                
        # Case 2 is complex because 'signal' doesn't tell us if it's an arb signal easily 
        # unless we pass that context. 
        # StrategyManager typically receives Single-Leg signals in this flow.
        # Multi-leg signals are handled via _handle_multi_leg_signal.
        # So we only handle "Breaking an Arb" here.
        
        return None

    def _resolve_conflict(self, symbol: str, signal: Dict[str, Any], new_strength: float) -> str:
        """
        Traffic Controller: Resolve conflicts between New Signal and Existing Position.
        
        Returns:
            'flip': Close existing, Open new.
            'upgrade': Close existing (profit), Open new (larger size).
            'block': Ignore new signal.
            'nuclear': Displace multi-leg.
        """
        
        # 1. Check Nuclear Displacement (Arb vs Single)
        nuclear = self._check_nuclear_displacement(symbol, new_strength)
        if nuclear == 'displacement_arb':
            return 'nuclear'
        elif nuclear == 'block':
            return 'block'

        # 2. Check Standard Position
        if symbol not in self.positions:
            return 'open' # No conflict
            
        position = self.positions[symbol]
        
        # Get Old Strength (Default to 0.5 if missing)
        # We need to ensure we store this on entry!
        old_strength = getattr(position, 'entry_signal_strength', 0.5) or 0.5
        
        # A. Opposing Signals (Long vs Short)
        is_opposing = (position.side == 'long' and signal['signal'] == 'sell') or \
                      (position.side == 'short' and signal['signal'] == 'buy')
                      
        if is_opposing:
            # Strength Hysteresis: New > Old * 1.3 (increased from 1.1 to reduce premature flips)
            if new_strength > (old_strength * 1.3):
                self.logger.info(f"🔄 FLIP Conflict: New ({float(new_strength or 0):.2f}) > Old ({float(old_strength or 0):.2f} * 1.3). Flipping.")
                return 'flip'
            else:
                self.logger.debug(f"🛡️ BLOCK Conflict: New ({float(new_strength or 0):.2f}) <= Old ({float(old_strength or 0):.2f} * 1.3). Holding.")
                return 'block'
                
        # B. Same Direction (Long vs Long)
        # Upgrade Only: New > Old + 0.5 (increased from 0.2 to prevent premature closures)
        if new_strength > (old_strength + 0.5):
            self.logger.info(f"⬆️ UPGRADE Conflict: New ({float(new_strength or 0):.2f}) > Old ({float(old_strength or 0):.2f} + 0.5). Resizing.")
            return 'upgrade'
            
        return 'block'


        
    def _force_close_position_for_displacement(self, symbol: str):
        """
        Force close a position (Single or Multi-Leg) due to Nuclear Displacement.
        """
        # Check Multi-Leg first
        # Use efficient lookup from Execution Engine
        arb_position = self.execution_engine.get_multi_leg_position_by_leg_symbol(symbol)
        
        if arb_position:
            self.logger.warning(f"☢️ EXECUTING NUCLEAR DISPLACEMENT on Arb {arb_position.position_id} for {symbol}")
            # Execute Exit Directly
            self.execution_engine.execute_multi_leg_exit(
                symbol=arb_position.primary_symbol,
                signal={'urgency': 'high', 'reason': 'nuclear_displacement'},
                strategy_name=arb_position.strategy,
                strategies_map=self.strategies
            )
            return

        # Check Single Leg
        if symbol in self.positions:
            self.logger.warning(f"☢️ EXECUTING NUCLEAR DISPLACEMENT on Single Leg {symbol}")
            self.close_position(symbol, reason='nuclear_displacement')

    def _should_execute_signal(self, symbol: str, signal: Dict[str, Any], current_price: float, 
                               ohlcv: Dict[str, pd.DataFrame], strategy_name: str) -> bool:
        """Determine if we should execute a trading signal."""
        
        # ---------------------------------------------------------
        # 1. Calculate Signal Strength & Modifiers (MOVED UP)
        # ---------------------------------------------------------
        base_strength = self.strategies[strategy_name].calculate_signal_strength(ohlcv, symbol=symbol, signal_context=signal)
        
        # Apply modifier from StrategySelector
        if hasattr(self.strategy_selector, 'get_signal_strength_modifier'):
            strength_modifier = self.strategy_selector.get_signal_strength_modifier(strategy_name)
            signal_strength = base_strength * strength_modifier
            if strength_modifier != 1.0:
                self.logger.info(f"applied signal strength modifier for {strategy_name}: {strength_modifier:.2f} (base: {float(base_strength or 0):.2f} -> {float(signal_strength or 0):.2f})")
        else:
            signal_strength = base_strength

        # ---------------------------------------------------------
        # 2. Conflict Resolution ("The Traffic Controller")
        # ---------------------------------------------------------
        resolution = self._resolve_conflict(symbol, signal, signal_strength)
        
        if resolution == 'block':
            # self.logger.debug(f"Signal for {symbol} BLOCKED by Traffic Controller")
            return False
            
        elif resolution == 'nuclear':
            self._force_close_position_for_displacement(symbol)
            return True
            # Proceed to open new trade logic handled by return True (Standard execution follows?)
            # Wait, if I return True, who calls execute_order for the NEW trade?
            # StrategyManager.execute_strategy checks if _should_execute returns True.
            # IF True, IT CALLS execute_order.
            # SO returning True is CORRECT.
            
        elif resolution in ['flip', 'upgrade']:
            self.close_position(symbol, reason=f'conflict_{resolution}')
            # Proceed to open new trade

        # ---------------------------------------------------------
        # 2a. Per-strategy cooldown to reduce churn
        # ---------------------------------------------------------
        cooldowns = (self.config.get("trading", {}) or {}).get("strategy_cooldowns", {}) or {}
        cooldown_sec = float(cooldowns.get(strategy_name, 0) or 0)
        last_ts = self._last_trade_ts_by_strategy.get(strategy_name)
        if cooldown_sec > 0 and last_ts:
            elapsed = (datetime.now() - last_ts).total_seconds()
            if elapsed < cooldown_sec:
                self.logger.info(
                    f"Cooldown active for {strategy_name}: waited {elapsed:.1f}s of {cooldown_sec:.0f}s"
                )
                return False

        # ---------------------------------------------------------
        # 2b. Pair blacklist/penalty
        # ---------------------------------------------------------
        trading_cfg = self.config.get("trading", {}) or {}
        pair_blacklist = set(trading_cfg.get("pair_blacklist", []) or [])
        if symbol in pair_blacklist:
            self.logger.info(f"Skipping {symbol} for {strategy_name}: pair is blacklisted")
            return False

        pair_penalties = trading_cfg.get("pair_penalties", {}) or {}
        penalty_scale = float(pair_penalties.get(symbol, 1.0) or 1.0)
        if penalty_scale < 1.0:
            signal_strength = signal_strength * penalty_scale
            self.logger.info(
                f"Applying pair penalty for {symbol}: scale={penalty_scale:.2f}, strength -> {float(signal_strength or 0):.2f}"
            )

        # ---------------------------------------------------------
        # 2c. Cost/edge hurdle (if signal provides expected edge in bps)
        # ---------------------------------------------------------
        cost_hurdles = (self.config.get("risk_management", {}) or {}).get("cost_hurdles", {}) or {}
        min_edge_bps = cost_hurdles.get(strategy_name)
        if min_edge_bps is not None:
            edge_bps = signal.get("expected_edge_bps", signal.get("edge_bps"))
            if edge_bps is not None and float(edge_bps) < float(min_edge_bps):
                self.logger.info(
                    f"Skipping {strategy_name} on {symbol}: edge {edge_bps}bps < hurdle {min_edge_bps}bps"
                )
                return False

        # ---------------------------------------------------------
        # 3. Standard Checks (Volatility, Limits)
        # ---------------------------------------------------------
        
        # Volatility calc - extract preferred dataframe or use one
        # Use strategy's timeframe if possible, or fallback
        strategy = self.strategies.get(strategy_name)
        if strategy and hasattr(strategy, 'timeframe') and strategy.timeframe in ohlcv:
            vol_df = ohlcv[strategy.timeframe]
        elif ohlcv:
            vol_df = next(iter(ohlcv.values()))
        else:
            vol_df = pd.DataFrame()
            
        market_volatility = self._calculate_market_volatility(vol_df)
        
        # Check position limit before proceeding
        # Note: If we just closed a position (flip/upgrade), the position count checks 
        # inside _should_execute_with_position_limit might still see the old position 
        # if execution engine hasn't fully cleared it from memory yet.
        # However, close_position() usually updates state immediately.
        # Bypass check for replacement operations
        if resolution not in ['flip', 'upgrade', 'nuclear']:
            if not self._should_execute_with_position_limit(symbol, signal, signal_strength):
                return False
        
        # Check per-strategy position limit
        strategy_position_count = self._count_positions_for_strategy(strategy_name)
        if strategy_position_count >= self.max_positions_per_strategy:
            self.logger.info(f"⛔ {strategy_name} has {strategy_position_count} positions (limit: {self.max_positions_per_strategy}), skipping {symbol}")
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
            f"signal_strength={float(signal_strength or 0):.2f}, volatility={float(market_volatility or 0):.2f}"
        )
        
        position_size, margin_required, leverage = self.leverage_manager.calculate_leveraged_position_size(
            symbol, current_price, available_capital_for_trade, strategy_name, signal_strength, market_volatility
        )
        
        self.logger.info(f"Position calculation for {symbol}: size={position_size:.4f}, margin=${float(margin_required or 0):.2f}, leverage={float(leverage or 0):.1f}x")
        
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
    
    def _execute_trade(self, symbol: str, signal: Dict[str, Any], current_price: float, strategy_name: str, ohlcv: Dict[str, pd.DataFrame], timestamp: datetime = None):
        """Execute a trade based on signal."""
        # Delegate to execution engine
        self.execution_engine.execute_trade(symbol, signal, current_price, strategy_name, ohlcv, self.strategies, timestamp=timestamp)
    
    def _count_positions_for_strategy(self, strategy_name: str) -> int:
        """
        Count the number of open positions for a specific strategy.
        
        Args:
            strategy_name: Name of the strategy to count positions for
            
        Returns:
            Number of open positions opened by this strategy
        """
        count = 0
        for symbol, pos in self.positions.items():
            # Position might be a Position object or a dict
            if hasattr(pos, 'strategy'):
                pos_strategy = pos.strategy
            elif isinstance(pos, dict):
                pos_strategy = pos.get('strategy', '')
            else:
                pos_strategy = ''
            
            # Match exact strategy name
            if pos_strategy == strategy_name:
                count += 1
        return count
    
    def close_position(self, symbol: str, reason: str = "manual", timestamp: datetime = None) -> Tuple[bool, str]:

        """Close a position and record it."""
        return self.execution_engine.close_position(symbol, reason, timestamp=timestamp)

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
        
        # ===== 4. Time & Momentum Score (15%) =====
        # Time factor - newer positions get protection (let them breathe)
        time_open = (datetime.now() - position.entry_time).total_seconds() / 3600  # hours
        
        # Protect very new positions (< 1h)
        if time_open < 1:
            time_score = 1.0  # Protected
        elif time_open < 6:
            time_score = 0.8  # Still fresh
        elif time_open < 12:
            time_score = 0.6  # Maturing
        elif time_open < 24:
            time_score = 0.4  # Getting stale
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
        
        time_momentum_score = (time_score * 0.6) + (momentum_score * 0.4)
        
        # ===== Final Score Calculation =====
        # Weights:
        # PnL: 30% (Performance is king)
        # Strategy: 30% (Trust the best strategies)
        # EV: 25% (Risk/Reward quality)
        # Time/Mom: 15% (Recency bias reduced)
        final_score = (
            (pnl_score * 0.30) +
            (strategy_score * 0.30) +
            (ev_score * 0.25) +
            (time_momentum_score * 0.15)
        )
        
        self.logger.debug(
            f"Position score for {symbol}: PnL={float(pnl_score or 0):.2f}, EV={float(ev_score or 0):.2f}, "
            f"Strategy={float(strategy_score or 0):.2f}, TimeMom={float(time_momentum_score or 0):.2f}, Final={float(final_score or 0):.2f}"
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
            self.logger.info(f"  #{rank} {sym} ({strategy}): score={float(score or 0):.3f}, PnL=${float(pnl or 0):.2f}")
        self.logger.info(f"  New signal strength: {float(new_signal_strength or 0):.3f}")
        self.logger.info("=" * 45)
        
        # Find the position with the lowest score
        least_profitable_symbol = min(position_scores.keys(), key=lambda x: position_scores[x])
        least_profitable_score = position_scores[least_profitable_symbol]
        least_profitable_pos = self.positions.get(least_profitable_symbol)
        
        # Scores are now 0-1 normalized, so use absolute thresholds
        # Lowered thresholds to facilitate rotation:
        # At position limit: new signal must be at least 0.05 points higher
        # Below limit: new signal must be at least 0.10 points higher
        
        at_limit = self._check_position_limit()
        threshold = 0.05 if at_limit else 0.10
        
        score_difference = new_signal_strength - least_profitable_score
        
        if score_difference > threshold:
            reason = "position_limit" if at_limit else "capital_rotation"
            pnl_value = least_profitable_pos.unrealized_pnl if least_profitable_pos and least_profitable_pos.unrealized_pnl is not None else 0.0
            self.logger.info(
                f"{'⚡ CAPITAL ROTATION' if at_limit else '🔄 CAPITAL ROTATION'}: "
                f"Closing {least_profitable_symbol} ({least_profitable_pos.strategy if least_profitable_pos else 'unknown'}) "
                f"[score={float(least_profitable_score or 0):.3f}, PnL=${float(pnl_value or 0):.2f}] "
                f"for new trade [strength={float(new_signal_strength or 0):.3f}] "
                f"(diff={float(score_difference or 0):.3f} > threshold={float(threshold or 0):.2f})"
            )
            self.close_position(least_profitable_symbol, reason)
            return True
        else:
            self.logger.info(
                f"❌ Capital rotation rejected: {least_profitable_symbol} score ({float(least_profitable_score or 0):.3f}) "
                f"+ threshold ({float(threshold or 0):.2f}) > new signal ({float(new_signal_strength or 0):.3f})"
            )
        
        return False
    
    def _should_displace_position(self, existing_score: float, new_strength: float) -> bool:
        """
        Shared threshold logic for position displacement decisions.
        
        Used by both single-leg capital rotation and multi-leg conflict resolution.
        
        Args:
            existing_score: Profitability score of existing position (0-1)
            new_strength: Signal strength of new potential trade (0-1)
            
        Returns:
            True if new position should displace existing, False otherwise
        """
        at_limit = self._check_position_limit()
        threshold = 0.05 if at_limit else 0.10
        return new_strength > existing_score + threshold
    
    def _get_multi_leg_profitability_score(self, position) -> float:
        """
        Calculate profitability score for a multi-leg position.
        
        Adapts _get_position_profitability_score logic for multi-leg positions.
        
        Score components:
        - Current PnL (30%): Combined unrealized P&L across all legs
        - Strategy Performance (30%): Historical win rate and profit factor
        - Expected Value (25%): Spread convergence potential
        - Time & Momentum (15%): Position age and spread momentum
        
        Args:
            position: MultiLegPosition object
            
        Returns:
            Profitability score (0-1, higher = more profitable to keep)
        """
        # ===== 1. Current PnL Score (30%) =====
        pnl = position.unrealized_pnl or 0.0
        capital_at_risk = position.capital_at_risk or position.total_notional or 1.0
        
        if capital_at_risk > 0:
            pnl_percentage = (pnl / capital_at_risk) * 100
        else:
            pnl_percentage = 0.0
        
        # Normalize PnL to 0-1 scale (cap at +/- 10%)
        pnl_score = max(-1.0, min(1.0, pnl_percentage / 10.0))
        pnl_score = (pnl_score + 1.0) / 2.0  # Shift to 0-1 range
        
        # ===== 2. Strategy Performance Score (30%) =====
        strategy_score = 0.5  # Default neutral
        strategy_name = position.strategy
        
        if strategy_name and self.performance_tracker:
            try:
                strategy_stats = self.performance_tracker.db.get_strategy_stats(strategy_name)
                
                total_trades = strategy_stats.get('total_trades', 0) or 0
                winning_trades = strategy_stats.get('winning_trades', 0) or 0
                if total_trades >= 5:
                    win_rate = winning_trades / total_trades
                    win_rate_score = win_rate
                else:
                    win_rate_score = 0.5
                
                gross_profit = strategy_stats.get('gross_profit', 0) or 0
                gross_loss = strategy_stats.get('gross_loss', 0) or 1
                profit_factor = gross_profit / max(gross_loss, 0.01)
                pf_score = min(1.0, profit_factor / 2.0)
                
                weight_score = 0.5
                if self.strategy_selector and strategy_name in self.strategy_selector.strategy_rankings:
                    ranking = self.strategy_selector.strategy_rankings[strategy_name]
                    weight_score = min(1.0, ranking.weight)
                
                strategy_score = (win_rate_score * 0.4) + (pf_score * 0.3) + (weight_score * 0.3)
                
            except Exception as e:
                self.logger.debug(f"Could not get strategy stats for {strategy_name}: {e}")
        
        # ===== 3. Expected Value Score (25%) =====
        # For multi-leg (stat_arb), EV is about spread convergence
        ev_score = 0.5  # Default neutral
        
        # If position is profitable, it's likely converging
        if pnl > 0:
            ev_score = min(1.0, 0.5 + (pnl_percentage / 20.0))
        else:
            ev_score = max(0.0, 0.5 + (pnl_percentage / 20.0))
        
        # ===== 4. Time & Momentum Score (15%) =====
        time_open = (datetime.now() - position.entry_time).total_seconds() / 3600  # hours
        
        if time_open < 1:
            time_score = 1.0  # Protected
        elif time_open < 6:
            time_score = 0.8
        elif time_open < 12:
            time_score = 0.6
        elif time_open < 24:
            time_score = 0.4
        else:
            time_score = 0.2
        
        # Momentum based on P&L direction
        if pnl > 0:
            momentum_score = min(1.0, 0.5 + abs(pnl_percentage) / 10.0)
        else:
            momentum_score = max(0.0, 0.5 - abs(pnl_percentage) / 10.0)
        
        time_momentum_score = (time_score * 0.6) + (momentum_score * 0.4)
        
        # ===== Final Score =====
        final_score = (
            (pnl_score * 0.30) +
            (strategy_score * 0.30) +
            (ev_score * 0.25) +
            (time_momentum_score * 0.15)
        )
        
        self.logger.debug(
            f"Multi-leg score for {position.position_id}: PnL={pnl_score:.2f}, "
            f"Strategy={strategy_score:.2f}, EV={ev_score:.2f}, "
            f"TimeMom={time_momentum_score:.2f}, Final={final_score:.2f}"
        )
        
        return final_score
    
    def _check_portfolio_allocation(self) -> Dict[str, Any]:
        """
        Check the current portfolio allocation to ensure we're not exceeding limits.
        
        Returns:
            Dictionary with allocation information
        """
        total_capital_at_risk = 0.0
        position_details = {}
        
        # Use Total Equity as the denominator for risk % calculation
        # Risk should be % of Account Equity, NOT % of Free Margin
        total_equity = self.portfolio_manager.total_equity
        
        for symbol, position in self.positions.items():
            # Use the capital_at_risk from the position if available, otherwise calculate it
            if position.capital_at_risk is not None:
                capital_at_risk = position.capital_at_risk
            else:
                # Fallback calculation
                notional_value = position.size * position.entry_price
                
                # If leverage is known, approximate margin usage
                if hasattr(position, 'leverage') and position.leverage > 0:
                    capital_at_risk = notional_value / position.leverage
                else:
                    # Worst case fallback: assume full notional risk (no leverage or 1x)
                    capital_at_risk = notional_value
            
            total_capital_at_risk += capital_at_risk
            
            # Handle division by zero
            if total_equity <= 0:
                percentage = 0.0
            else:
                percentage = (capital_at_risk / total_equity) * 100
            
            position_details[symbol] = {
                'value': capital_at_risk,
                'percentage': percentage,
                'notional_value': position.size * position.entry_price
            }
        
        # Handle division by zero
        if total_equity <= 0:
            allocation_percentage = 0.0
        else:
            allocation_percentage = (total_capital_at_risk / total_equity) * 100
        
        return {
            'total_capital_at_risk': total_capital_at_risk,
            'available_capital': self.portfolio_manager.calculate_available_capital_for_trading(),
            'total_equity': total_equity,
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

        alloc_pct = float(allocation.get('allocation_percentage') or 0.0)
        self.logger.info(
            f"Portfolio allocation for {symbol}: {alloc_pct:.1f}% "
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

    def load_positions_from_db(self):
        """Load positions from database."""
        return self.execution_engine.load_positions_from_db()
    
    def check_startup_exits(self):
        """
        Check TP/SL for all loaded positions on startup.
        
        Ghost positions loaded from DB may already be past their TP/SL levels.
        This ensures we close them immediately rather than waiting for the next monitoring cycle.
        """
        if not self.positions:
            return
        
        positions_to_check = list(self.positions.items())  # Copy to avoid dict mutation during iteration
        
        for symbol, position in positions_to_check:
            try:
                # Get current price
                price = self.market_api.get_current_price(symbol)
                if not price:
                    self.logger.warning(f"Cannot check startup exit for {symbol}: no price available")
                    continue
                
                # Update position's current price
                position.current_price = price
                
                # Check if exit conditions are met
                close_reason = self._should_close_position(position)
                if close_reason:
                    self.logger.warning(f"🚨 Position {symbol} meets {close_reason} on startup (price=${price:.2f}), closing immediately...")
                    self.close_position(symbol, close_reason)
                    
            except Exception as e:
                self.logger.error(f"Error checking startup exit for {symbol}: {e}")
    
    def update_position_prices(self):
        """Update current prices and margin info for all open positions."""
        now = time.time()
        
        # Update single-leg positions
        for symbol, position in self.positions.items():
            current_price = self.market_api.get_current_price(symbol)
            if current_price:
                position.current_price = current_price
            
            # Cache margin info periodically (every 30s per position)
            last_margin_update = getattr(position, '_last_margin_update', 0)
            if now - last_margin_update > 30:
                try:
                    margin_info = self.market_api.get_position_margin_info(symbol)
                    if margin_info:
                        position.liquidation_price = margin_info.get('liquidation_price')
                        position.margin_used = margin_info.get('margin_used')
                        position._last_margin_update = now
                except Exception as e:
                    self.logger.debug(f"Error caching margin info for {symbol}: {e}")
        
        # Update multi-leg positions
        for position_id, position in self.multi_leg_positions.items():
            for leg in position.legs:
                leg_price = self._get_leg_price(leg.symbol, leg.market_type)
                if leg_price:
                    position.current_prices[leg.symbol] = leg_price
                    leg.current_price = leg_price  # Cache on leg object for dashboard
    
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
                    'current_price': position.current_prices.get(leg.symbol),
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
        alloc_pct = float(allocation.get('allocation_percentage') or 0.0)
        self.logger.info(f"Portfolio Allocation: {alloc_pct:.1f}% / {allocation['max_allocation']}%")
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
                entry_price_val = leg.get('entry_price') or 0
                current_price_val = leg.get('current_price') or 0
                self.logger.info(f"   └─ {leg['market_type']:<5} {leg['side']:<5} {leg['size']:>10.6f} {leg['symbol']:<12} @ ${float(entry_price_val or 0):.4f} → ${float(current_price_val or 0):.4f} | ${float(leg_pnl or 0):>8.2f} {leg_status}")
        
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

    def _monitor_and_close_positions(self, emergency_portfolio_loss_pct: float, timestamp: datetime = None):
        """Monitor positions and close them if they meet closure criteria."""
        try:
            current_time = time.time()
            
            self.logger.debug(f"Position monitoring: checking {len(self.positions)} positions")
            
            # Check for positions that need to be closed
            positions_to_close = []
            
            for symbol, position in self.positions.items():
                if not position.current_price:
                    # CRITICAL: Never silently skip - force fetch price with retry
                    self.logger.warning(f"⚠️ No price for open position {symbol}, forcing fetch...")
                    price = self._force_fetch_price_with_retry(symbol)
                    if price:
                        position.current_price = price
                    else:
                        self.logger.error(f"🚨 CRITICAL: Cannot price position {symbol} after retries!")
                        continue
                
                close_reason = self._should_close_position(position)
                if close_reason:
                    positions_to_close.append((symbol, close_reason))
            
            # Close positions
            for symbol, reason in positions_to_close:
                success, _ = self.close_position(symbol, reason, timestamp=timestamp)
                if success:
                    self.total_positions_closed += 1
            
            # Emergency stop check (every 30 seconds)
            if current_time - self.last_emergency_check >= 30:
                # Calculate current portfolio loss percentage
                if self._check_emergency_stop(emergency_portfolio_loss_pct):
                    self.emergency_stops_triggered += 1
                    self.logger.error("🚨 EMERGENCY STOP TRIGGERED - Closing all positions!")
                    self.close_all_positions("emergency_stop")
                    return
                self.last_emergency_check = current_time
            
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
    
    def _force_fetch_price_with_retry(self, symbol: str, max_retries: int = 3) -> Optional[float]:
        """
        Force fetch price with exponential backoff retries.
        
        Args:
            symbol: Trading symbol
            max_retries: Maximum retry attempts
            
        Returns:
            Price if successful, None otherwise
        """
        for attempt in range(max_retries):
            try:
                price = self.market_api.get_current_price(symbol)
                if price and price > 0:
                    return price
            except Exception as e:
                self.logger.warning(f"Price fetch attempt {attempt+1}/{max_retries} for {symbol}: {e}")
            
            if attempt < max_retries - 1:
                # Aggressive backoff: 2s, 10s, 30s
                backoff_steps = [2, 10, 30]
                time.sleep(backoff_steps[min(attempt, len(backoff_steps)-1)])
        
        return None
    
    
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
            # Check for positions on exchange that aren't tracked locally
            local_symbols = set(self.positions.keys())
            
            # Add symbols from multi-leg positions
            for ml_pos in self.multi_leg_positions.values():
                for leg in ml_pos.legs:
                    local_symbols.add(leg.symbol)
                    
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