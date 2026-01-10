
import logging
import time
import math
import random
import json
import os
from datetime import datetime, timedelta
from typing import Dict, List, Any, Optional
import pandas as pd

from src.models.trade import Trade, Position, MultiLegPosition, PositionLeg
from src.api import HyperliquidAPI
from src.utils.leverage_manager import LeverageManager
from src.utils.portfolio_manager import PortfolioManager
from src.utils.performance_tracker import PerformanceTracker
from src.utils.pair_selector import DynamicPairSelector
from src.utils.statistics import calculate_annualized_volatility

class ExecutionEngine:
    """
    Handles order execution, position management, and multi-leg coordination.
    Decoupled from strategy logic.
    """
    
    def __init__(
        self, 
        config: Dict[str, Any],
        market_api: HyperliquidAPI,
        leverage_manager: LeverageManager,
        portfolio_manager: PortfolioManager,
        performance_tracker: PerformanceTracker,
        pair_selector: DynamicPairSelector,
        strategy_selector: Any = None  # Added for live performance feedback loop
    ):
        self.config = config
        self.market_api = market_api
        self.leverage_manager = leverage_manager
        self.portfolio_manager = portfolio_manager
        self.performance_tracker = performance_tracker
        self.pair_selector = pair_selector
        self.strategy_selector = strategy_selector
        
        self.logger = logging.getLogger(__name__)
        
        # Trading state managed by Execution Engine
        self.positions: Dict[str, Position] = {}  # symbol -> Position
        self.multi_leg_positions: Dict[str, MultiLegPosition] = {}  # position_id -> MultiLegPosition
        self.trades: List[Trade] = []
        
        # Statistics
        self.total_trades = 0
        self.total_pnl = 0.0
        self.winning_trades = 0
        
        # Load persisted positions if available
        # Load persisted positions if available
        self.load_positions_from_db()
        
    def check_slippage_tolerance(self, symbol: str, current_price: float, volatility: float) -> bool:
        """
        Check if market conditions are safe for execution (simulate slippage/spread check).
        
        Args:
            symbol: Trading pair
            current_price: Current market price
            volatility: Current annualized volatility estimate
            
        Returns:
            True if safe to trade, False if slippage risk is too high
        """
        try:
            # 1. Volatility Trigger
            # If volatility is extremely high (> 150% annualized), spreads effectively widen
            # and execution becomes unpredictable.
            MAX_VOLATILITY = 1.50 # 150% annualized
            if volatility > MAX_VOLATILITY:
                return False
                
            # 2. Simulated Spread Check (Backtest proxy)
            # In live trading, we would check order book depth here.
            # In backtest/sim, we block trades if price is essentially zero or invalid
            if current_price <= 0:
                return False
                
            return True
        except Exception as e:
            self.logger.error(f"Error checking slippage for {symbol}: {e}")
            return True # Fail open safely? Or close? Let's default to True to not block unless certain.

    def execute_trade(self, symbol: str, signal: Dict[str, Any], current_price: float, strategy_name: str, ohlcv: Dict[str, pd.DataFrame], strategies_map: Dict[str, Any], timestamp: datetime = None):
        """Execute a single-leg trade based on signal."""
        try:
            current_time = timestamp if timestamp else datetime.now()
            
            # Determine trade side
            if signal['signal'] == 'buy':
                side = 'buy'
                position_side = 'long'
            elif signal['signal'] == 'sell':
                side = 'sell'
                position_side = 'short'
            else:
                return
            
            # Check slippage tolerance
            market_volatility = signal.get('market_volatility', 0.0)
            if not self.check_slippage_tolerance(symbol, current_price, market_volatility):
                self.logger.warning(f"Slippage check failed for {symbol} (volatility={market_volatility:.4f})")
                return
            
            # Get leverage and position details
            # 1. Fetch Asset Limits
            asset_meta = self.market_api.get_asset_meta(symbol)
            asset_max_leverage = float(asset_meta.get('maxLeverage', 100.0)) if asset_meta else 100.0
            
            # 2. Ensure Market Volatility (Annualized) & Context
            market_volatility = signal.get('market_volatility', 0.0)
            if market_volatility <= 0.0:
                 market_volatility = self.calculate_market_volatility(ohlcv)
                 signal['market_volatility'] = market_volatility # Backfill for context

            signal_strength = signal['signal_strength']
            
            # 3. Pre-Calculate Stop Loss (to enable Risk-Based Sizing)
            strategy = strategies_map[strategy_name]
            
            # Build signal context
            signal_context = {
                'z_score': signal.get('z_score'),
                'sigma': signal.get('sigma'),
                'mu': signal.get('mu'),
                'market_volatility': market_volatility,
                'signal_strength': signal_strength,
            }
            
            # Check for Strategy-specific stop loss
            strategy_stop_loss = None
            stop_loss_pct_for_sizing = 0.0
            
            if hasattr(strategy, 'calculate_stop_loss') and callable(getattr(strategy, 'calculate_stop_loss')):
                strategy_stop_loss = strategy.calculate_stop_loss(current_price, side, signal_context)
                
                if strategy_stop_loss is not None and current_price > 0:
                     # Calculate implied percentage
                     stop_loss_pct_for_sizing = abs(current_price - strategy_stop_loss) / current_price
            
            # 4. Calculate Sizing & Leverage
            # We pass the calculated stop_loss_pct so LeverageManager can size based on risk budget.
            # If stop_loss_pct is 0.0, LeverageManager will infer one based on leverage (safety fallback).
            
            position_size, margin_required, leverage = self.leverage_manager.calculate_leveraged_position_size(
                symbol, current_price, 
                self.portfolio_manager.calculate_available_capital_for_trading() if self.portfolio_manager else 10000,
                strategy_name, signal['signal_strength'], market_volatility,
                asset_max_leverage=asset_max_leverage,
                stop_loss_pct=stop_loss_pct_for_sizing
            )
            # Explicitly cast to int as exchange requires integer leverage
            leverage = int(leverage)
            
            # 5. Enforce Leverage on Exchange
            try:
                # Check current leverage state
                current_positions = self.market_api.get_positions()
                current_pos = next((p for p in current_positions if p['symbol'] == symbol), None)
                
                needs_update = True
                if current_pos:
                    # Parse current leverage from position data
                    curr_lev_data = current_pos.get('leverage', {})
                    if isinstance(curr_lev_data, dict):
                        curr_lev_val = int(curr_lev_data.get('value', -1))
                    else:
                        curr_lev_val = int(curr_lev_data)
                        
                    if curr_lev_val == leverage:
                        needs_update = False
                        
                if needs_update:
                    self.market_api.update_leverage(symbol, leverage, is_cross=True)
                    
            except Exception as e:
                self.logger.warning(f"Leverage update check failed for {symbol}: {e}. Proceeding with leverage {leverage}x.")

            signal_strength = signal['signal_strength']
            market_volatility = signal['market_volatility']
            
            # 7. Final Stop Loss Determination (Account Based Fallback)
            # Note: strategy_stop_loss is already calculated above for sizing.
            # We revisit it here to ensure it respects account safety limits logic.
            
            
            # 2. Global safety net: Max X% of account value loss per trade
            account_equity = self.portfolio_manager.total_equity if self.portfolio_manager else 10000
            max_account_loss_pct = self.config['trading'].get('max_account_loss_per_trade', 3.0) / 100
            max_account_loss = account_equity * max_account_loss_pct
            
            max_price_change = max_account_loss / position_size if position_size > 0 else current_price * 0.05
            
            if position_side == 'long':
                stop_loss_account_based = current_price - max_price_change
            else:
                stop_loss_account_based = current_price + max_price_change
            
            # 3. Leverage-based stop loss
            stop_loss_leverage_based = self.leverage_manager.calculate_stop_loss_with_leverage(
                current_price, position_side, leverage
            )
            
            # Use the MOST CONSERVATIVE stop loss
            # Logic Update: If strategy provides explicit SL, use it (subject to account safety).
            # If not, use generic leverage-based fallback.
            # "Account Based" (Hard Risk Cap) always applies as the worst-case floor/ceiling.
            
            if position_side == 'long':
                if strategy_stop_loss is not None:
                     # Strategy knows best, but don't exceed account safety
                     stop_loss = max(strategy_stop_loss, stop_loss_account_based)
                else:
                     # Use tightest of leverage-fallback vs account-safety
                     stop_loss = max(stop_loss_leverage_based, stop_loss_account_based)
            else:
                if strategy_stop_loss is not None:
                     stop_loss = min(strategy_stop_loss, stop_loss_account_based)
                else:
                     stop_loss = min(stop_loss_leverage_based, stop_loss_account_based)
            
            # Avoid formatting errors when strategy_stop_loss is None
            strategy_stop_loss_display = (
                "deferred" if strategy_stop_loss is None else f"{strategy_stop_loss:.4f}"
            )
            self.logger.debug(
                f"Stop loss calculation for {symbol}: Final={stop_loss:.4f} "
                f"(strategy={strategy_stop_loss_display})"
            )
            
            # === TAKE PROFIT CALCULATION ===
            strategy_take_profit = strategy.calculate_take_profit(
                current_price, side, ohlcv, signal_strength, market_volatility
            )
            
            take_profit = self.leverage_manager.calculate_take_profit_with_leverage(
                current_price, position_side, leverage, strategy_take_profit
            )
            
            # Capital-based take profit
            target_profit_amount = margin_required * 1.0
            take_profit_capital_based = self.leverage_manager.calculate_take_profit_with_capital_at_risk(
                current_price, position_side, margin_required, target_profit_amount
            )
            
            # Use more conservative take profit
            if position_side == 'long':
                take_profit = min(take_profit, take_profit_capital_based)
            else:
                take_profit = max(take_profit, take_profit_capital_based)
            
            # Execute order
            order_result = self.market_api.execute_order(
                symbol=symbol,
                side=side,
                size=position_size,
                reduce_only=False,
                urgency="normal"
            )
            
            if order_result and order_result.get('filled_size', 0) > 0:
                fill_size = order_result['filled_size']
                fill_price = float(order_result['avg_fill_price'])
                order_id = order_result.get('order_id')
                
                if order_result.get('status') == 'partial':
                    self.logger.warning(f"⚠ Partial fill for {symbol}: {fill_size}/{position_size}")
                
                # Create trade record
                trade = Trade(
                    symbol=symbol,
                    side=side,
                    size=fill_size,
                    price=fill_price,
                    timestamp=current_time,
                    strategy=strategy_name,
                    order_id=order_id,
                )
                
                # Get trailing stop config
                trailing_config = strategy.get_trailing_stop_config()
                
                # Create position record
                position = Position(
                    symbol=symbol,
                    side=position_side,
                    size=fill_size,
                    entry_price=fill_price,
                    entry_time=current_time,
                    strategy=strategy_name,
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                    capital_at_risk=margin_required,
                    leverage=leverage,
                    order_id=order_id,  # Exchange OID for traceability
                    trailing_stop_enabled=trailing_config.get('enabled', False),
                    trailing_stop_pct=trailing_config.get('trail_pct', 0.0),
                    trailing_stop_activation_pct=trailing_config.get('activation_pct', 0.0),
                    highest_price=fill_price if position_side == 'long' else None,
                    lowest_price=fill_price if position_side == 'short' else None,
                    trailing_stop_active=False,
                )
                
                # Record in leverage manager
                self.leverage_manager.record_position(
                    symbol, position_side, fill_size, fill_price, leverage, margin_required
                )
                
                self.positions[symbol] = position
                self.trades.append(trade)
                self.total_trades += 1
                
                self.logger.info(f"✅ Executed {side} trade for {symbol}: {fill_size} @ {fill_price:.6f}")
                
                # Save positions
                # Save positions
                self.save_positions_to_db()
                
            else:
                self.logger.error(f"Failed to fill order for {symbol}")
                
        except Exception as e:
            self.logger.error(f"Error executing trade for {symbol}: {e}")
    
    def close_position(self, symbol: str, reason: str = "manual", timestamp: datetime = None) -> bool:
        """Close a position and record it."""
        if symbol not in self.positions:
            self.logger.warning(f"No position to close for {symbol}")
            return False
            
        current_time = timestamp if timestamp else datetime.now()
        
        try:
            position = self.positions[symbol]
            close_side = 'sell' if position.side == 'long' else 'buy'
            urgency = "high" if reason in ['stop_loss', 'liquidation_risk', 'emergency'] else "normal"
            
            order_result = self.market_api.execute_order(
                symbol=symbol,
                side=close_side,
                size=position.size,
                reduce_only=True,
                urgency=urgency
            )
            
            if order_result and order_result.get('filled_size', 0) > 0:
                exit_price = order_result['avg_fill_price']
                filled_size = order_result['filled_size']
                
                # Calculate P&L
                pnl = (exit_price - position.entry_price) * filled_size
                if position.side == 'short':
                    pnl = -pnl
                
                # Retrieve Fee - Try from response first, then fetch from API if missing (common on mainnet)
                fee = order_result.get('total_fee', 0.0) if order_result else 0.0
                if fee == 0.0 and order_result.get('order_id'):
                     # The SDK parsed response might miss fee, so we fetch it explicitly
                     # Use the numeric OID from the fill
                     oid = order_result.get('fills', [{}])[0].get('oid') if order_result.get('fills') else None
                     if oid:
                        # Slight delay to ensure indexing
                        time.sleep(0.5) 
                        fee = self.market_api.get_execution_fee(oid)

                # Record trade
                self.performance_tracker.record_trade_from_position(
                    symbol=symbol,
                    strategy=position.strategy,
                    side=position.side,
                    entry_price=position.entry_price,
                    exit_price=exit_price,
                    size=filled_size,
                    entry_time=position.entry_time,
                    exit_time=current_time,
                    capital_at_risk=position.capital_at_risk,
                    exit_reason=reason,
                    stop_loss=position.stop_loss,
                    take_profit=position.take_profit,
                    leverage=position.leverage,
                    fees=fee
                )
                
                # Close in leverage manager
                self.leverage_manager.close_position(symbol, exit_price)
                
                # Delete from DB persistence
                try:
                    position_id = f"pos_{position.strategy}_{symbol}"
                    self.performance_tracker.db.delete_position(position_id)
                except Exception as e:
                    self.logger.error(f"Failed to delete position {position_id} from DB: {e}")

                # Remove from positions
                del self.positions[symbol]
                
                # Update stats
                self.total_pnl += pnl
                if pnl > 0:
                    self.winning_trades += 1
                
                self.pair_selector.update_pair_performance(symbol, pnl)
                
                # LIVE FEEDBACK LOOP: Report result to StrategySelector immediately
                if self.strategy_selector:
                    # Calculate percentage return relative to capital at risk (margin)
                    # If capital_at_risk is 0 or missing, fallback to 0
                    capital = position.capital_at_risk or 0.0
                    pct_return = (pnl / capital) if capital > 0 else 0.0
                    
                    self.strategy_selector.record_trade_result(
                        position.strategy, 
                        pct_return,
                        current_time
                    )

                self.logger.info(f"✅ Closed position for {symbol}: P&L ${pnl:.2f} ({reason})")
                return True
                
            return False
            
        except Exception as e:
            self.logger.error(f"Error closing position for {symbol}: {e}")
            return False

    def handle_multi_leg_signal(
        self, 
        symbol: str, 
        signal: Dict[str, Any], 
        current_price: float, 
        strategy_name: str, 
        ohlcv: Dict[str, pd.DataFrame],
        calculate_signal_strength_fn,
        strategies_map: Dict[str, Any],
        timestamp: datetime = None
    ):
        """Handle multi-leg signals (entry or exit)."""
        action = signal.get('action')
        
        if action == 'enter':
            signal_strength = calculate_signal_strength_fn(ohlcv, symbol=symbol, signal_context=signal)
            # Position limit check logic would need to be moved or passed in
            # Assuming simplified flow for now or we will add the method
            self.execute_multi_leg_entry(symbol, signal, current_price, strategy_name, ohlcv, signal_strength, timestamp)
        elif action == 'exit':
            self.execute_multi_leg_exit(symbol, signal, strategy_name, strategies_map, timestamp)
        # Also accept 'open'/'close' variants used by StatisticalArbitrageStrategy
        elif action == 'open':
            signal_strength = calculate_signal_strength_fn(ohlcv, symbol=symbol, signal_context=signal)
            self.execute_multi_leg_entry(symbol, signal, current_price, strategy_name, ohlcv, signal_strength, timestamp)
        elif action == 'close':
            self.execute_multi_leg_exit(symbol, signal, strategy_name, strategies_map, timestamp)
    
    def execute_multi_leg_entry(
        self, 
        symbol: str, 
        signal: Dict[str, Any],
        current_price: float,
        strategy_name: str,
        ohlcv: Dict[str, pd.DataFrame],
        signal_strength: float,
        timestamp: datetime = None
    ):
        """Execute a multi-leg position entry."""
        try:
            current_time = timestamp if timestamp else datetime.now()
            
            if self._get_multi_leg_position_for_symbol(symbol):
                self.logger.info(f"Already have multi-leg position for {symbol}, skipping")
                return
            
            market_volatility = self.calculate_market_volatility(ohlcv)
            available_capital = self.portfolio_manager.calculate_available_capital_for_trading()
            
            # Exploration logic omitted for brevity/cleanliness, can be re-injected if needed
            
            position_size, margin_required, leverage = self.leverage_manager.calculate_leveraged_position_size(
                symbol, current_price, float(available_capital), strategy_name, signal_strength, market_volatility
            )
            
            if position_size <= 0:
                return
                
            if not self.leverage_manager.can_open_position(symbol, margin_required, float(available_capital)):
                return
                
            notional_value = position_size * current_price
            legs = signal.get('legs', [])
            
            # =========================================================================
            # FUND ALLOCATION: Ensure funds are in the correct accounts
            # Hyperliquid has separate spot and perp accounts that require transfers
            # =========================================================================
            
            # Calculate required funds for each account type
            perp_required = 0.0
            spot_required = 0.0
            
            for leg_spec in legs:
                leg_market_type = leg_spec['market_type']
                leg_symbol = leg_spec['symbol']
                leg_price = self.get_leg_price(leg_symbol, leg_market_type)
                
                if not leg_price or leg_price <= 0:
                    self.logger.error(f"Cannot get price for leg {leg_symbol} ({leg_market_type}) during fund allocation")
                    return
                
                leg_notional = notional_value  # Each leg gets full notional for delta-neutral
                
                if leg_market_type in ('perp', 'hip3'):
                    # Perp requires margin (notional / leverage)
                    # Count perp legs to split available margin evenly? Or just assumes each needs full margin?
                    # Original logic divided by number of perp legs
                    perp_legs_count = len([l for l in legs if l.get('market_type') in ('perp', 'hip3')])
                    if perp_legs_count > 0:
                        perp_required += margin_required / perp_legs_count
                elif leg_market_type == 'spot':
                    # Spot requires full notional (buying the asset)
                    spot_required += leg_notional
            
            # Add buffer for slippage and fees (2%)
            perp_required *= 1.02
            spot_required *= 1.02
            
            self.logger.info(f"Fund allocation: perp=${perp_required:.2f}, spot=${spot_required:.2f}")

            # If combined USDC (spot + perp withdrawable) is insufficient, scale down the trade to fit.
            try:
                ml_cfg = (self.config.get("trading", {}) or {}).get("multi_leg", {}) or {}
                if ml_cfg.get("auto_scale_to_funds", True):
                    total_required = float(perp_required) + float(spot_required)
                    if total_required > 0:
                        spot_usdc = float(self.market_api.get_spot_balance("USDC") or 0.0)
                        perp_withdrawable = float((self.market_api.get_perp_balance() or {}).get("withdrawable", 0.0) or 0.0)
                        total_available = max(0.0, spot_usdc) + max(0.0, perp_withdrawable)

                        if total_available + 1e-6 < total_required:
                            scale = total_available / total_required if total_required > 0 else 0.0
                            min_scale = float(ml_cfg.get("min_scale_factor", 0.10) or 0.0)

                            if scale < min_scale:
                                self.logger.warning(
                                    f"Multi-leg entry for {symbol} blocked: insufficient combined USDC. "
                                    f"required=${total_required:.2f}, available=${total_available:.2f}, "
                                    f"scale={scale:.3f} < min_scale={min_scale:.2f}"
                                )
                                return

                            self.logger.warning(
                                f"Scaling multi-leg entry for {symbol} to available funds: "
                                f"required=${total_required:.2f}, available=${total_available:.2f}, scale={scale:.3f}"
                            )

                            # All sizing components are linear in notional -> scale proportionally.
                            position_size = float(position_size) * scale
                            margin_required = float(margin_required) * scale
                            notional_value = float(notional_value) * scale
                            perp_required = float(perp_required) * scale
                            spot_required = float(spot_required) * scale
            except Exception as e:
                self.logger.debug(f"Multi-leg auto-scale-to-funds failed (continuing without scaling): {e}")
            
            # Ensure perp account has funds
            if perp_required > 0:
                if not self.market_api.ensure_perp_funds(perp_required):
                    self.logger.error(f"Cannot allocate ${perp_required:.2f} to perp account")
                    return
            
            # Ensure spot account has funds
            if spot_required > 0:
                if not self.market_api.ensure_spot_funds(spot_required):
                    self.logger.error(f"Cannot allocate ${spot_required:.2f} to spot account")
                    return
            
            self.logger.info("Fund allocation complete, executing legs...")
            
            # Execute legs
            executed_legs = []
            is_atomic = signal.get('atomic', True)
            
            for i, leg_spec in enumerate(legs):
                leg_symbol = leg_spec['symbol']
                leg_market_type = leg_spec['market_type']
                leg_order_side = leg_spec['order_side']
                leg_reduce_only = leg_spec.get('reduce_only', False)
                
                leg_price = self.get_leg_price(leg_symbol, leg_market_type)
                if not leg_price:
                    if is_atomic: self.unwind_executed_legs(executed_legs)
                    return
                
                # Respect per-leg hedge_ratio for proportional sizing
                leg_hedge_ratio = leg_spec.get('hedge_ratio', 1.0)
                leg_size = (notional_value * leg_hedge_ratio) / leg_price
                
                result = self.market_api.execute_order(
                    symbol=leg_symbol,
                    side=leg_order_side,
                    size=leg_size,
                    reduce_only=leg_reduce_only,
                    urgency="normal",
                    market_type=leg_market_type
                )
                
                if result and result.get('filled_size', 0) > 0:
                    executed_legs.append(PositionLeg(
                        symbol=leg_symbol,
                        market_type=leg_market_type,
                        side=leg_spec['side'],
                        size=result['filled_size'],
                        entry_price=result['avg_fill_price'],
                        order_id=result.get('order_id'),
                    ))
                    self.logger.debug(f"  Leg executed: {leg_symbol} {leg_spec['side']} x {result['filled_size']} @ {result['avg_fill_price']}")
                else:
                    if is_atomic: self.unwind_executed_legs(executed_legs)
                    return
            
            # Calculate composite entry price (Weighted Average of fills)
            # Note: For StatArb spreading, this "price" is synthetic but better than 0.0 or 1.0
            # Ideally we track PnL per leg, but for the 'Head' position in DB we need a number.
            total_notional_filled = sum(l.size * l.entry_price for l in executed_legs)
            total_size_filled = sum(l.size for l in executed_legs)
            avg_entry_price = total_notional_filled / total_size_filled if total_size_filled > 0 else 0.0

            # Create position
            position_id = f"{strategy_name}_{symbol}_{int(current_time.timestamp() * 1000)}"
            multi_leg_position = MultiLegPosition(
                position_id=position_id,
                strategy=strategy_name,
                entry_time=current_time,
                legs=executed_legs,
                capital_at_risk=margin_required,
                metadata=signal.get('metadata', {}),
            )
            
            # CRITICAL FIX: Ensure position has correct entry price for DB/PnL
            # (Note: MultiLegPosition calculates properties from legs, but we can set metadata if needed)
            
            self.multi_leg_positions[position_id] = multi_leg_position
            
            # Record composite position for margin checks
            self.leverage_manager.record_position(symbol, 'multi_leg', position_size, avg_entry_price, leverage, margin_required)
            
            self.logger.info(f"✅ Multi-leg position opened: {position_id} (Composite Price: {avg_entry_price:.6f})")
            
        except Exception as e:
            self.logger.error(f"Error executing multi-leg entry for {symbol}: {e}")
            
    def execute_multi_leg_exit(self, symbol: str, signal: Dict[str, Any], strategy_name: str, strategies_map: Dict[str, Any], timestamp: datetime = None):
        """Execute multi-leg exit."""
        try:
            current_time = timestamp if timestamp else datetime.now()
            
            position = self._get_multi_leg_position_for_symbol(symbol)
            if not position:
                return
                
            urgency = signal.get('urgency', 'normal')
            
            exit_results = []
            for leg in position.legs:
                order_side = 'sell' if leg.side == 'long' else 'buy'
                
                result = self.market_api.execute_order(
                    symbol=leg.symbol,
                    side=order_side,
                    size=leg.size,
                    reduce_only=leg.market_type == 'perp',
                    urgency=urgency,
                    market_type=leg.market_type
                )
                
                if result:
                    # Get fee from result, falling back to 0.0
                    fee = float(result.get('fee', result.get('total_fee', 0.0)))
                    
                    # If fee is 0 or missing, try to fetch it authoritatively
                    if fee == 0.0 and result.get('order_id'):
                        try:
                            # Wait a brief moment for fill to index
                            time.sleep(0.5)
                            fetched_fee = self.market_api.get_execution_fee(result['order_id'])
                            if fetched_fee > 0:
                                fee = fetched_fee
                                self.logger.info(f"Retrieved authoritative fee for multi-leg exit {leg.symbol}: {fee}")
                        except Exception as e:
                            self.logger.warning(f"Failed to fetch fee for multi-leg exit {leg.symbol}: {e}")

                    exit_results.append({
                        'leg': leg,
                        'exit_price': result['avg_fill_price'],
                        'filled_size': result['filled_size'],
                        'fee': fee
                    })
            
            # Calculate P&L and total fees
            total_pnl = 0.0
            total_fees = 0.0
            for result in exit_results:
                leg = result['leg']
                exit_price = result['exit_price']
                price_diff = exit_price - leg.entry_price
                if leg.side == 'short':
                    price_diff = -price_diff
                leg_pnl = price_diff * result['filled_size']
                total_pnl += leg_pnl
                total_fees += result.get('fee', 0.0)

            exit_time = current_time

            # For funding rate arbitrage we store realized PnL as funding payments only (delta-neutral)
            # and represent side as 'delta_neutral' in the DB.
            pnl_to_record = total_pnl
            trade_side = "multi_leg"
            if strategy_name == "funding_rate_arbitrage":
                pnl_to_record = self.estimate_funding_arb_realized_pnl(position, exit_time, strategies_map)
                trade_side = "delta_neutral"

            # Record trade in performance tracker
            self.performance_tracker.record_trade_from_position(
                symbol=position.primary_symbol,
                strategy=strategy_name,
                side=trade_side,
                entry_price=sum(leg.entry_price * leg.size for leg in position.legs) / sum(leg.size for leg in position.legs) if position.legs and sum(leg.size for leg in position.legs) > 0 else 0,
                exit_price=sum(r['exit_price'] * r['filled_size'] for r in exit_results) / sum(r['filled_size'] for r in exit_results) if exit_results and sum(r['filled_size'] for r in exit_results) > 0 else 0,
                size=sum(r['filled_size'] for r in exit_results),
                entry_time=position.entry_time,
                exit_time=exit_time,
                capital_at_risk=position.capital_at_risk or 0,
                exit_reason=signal.get('reason', 'signal'),
                pnl_override=pnl_to_record,
                stop_loss=position.legs[0].to_dict().get('metadata', {}).get('stop_loss') if position.legs and position.legs[0].market_type == 'perp' else None,
                fees=total_fees
            )

            # LIVE FEEDBACK LOOP: Report result to StrategySelector immediately
            # This updates the strategy's recent performance window for dynamic signal sizing
            if self.strategy_selector:
                self.strategy_selector.record_trade_result(
                    strategy_name, 
                    pnl_to_record / position.initial_capital if position.initial_capital > 0 else 0,
                    exit_time
                )

            # Close position in leverage manager
            self.leverage_manager.close_position(position.primary_symbol, 0)
            
            # Delete from DB persistence
            try:
                self.performance_tracker.db.delete_position(position.position_id)
            except Exception as e:
                self.logger.error(f"Failed to delete multi-leg position {position.position_id} from DB: {e}")

            # Remove from active positions
            del self.multi_leg_positions[position.position_id]
            
            # Update stats
            self.total_pnl += pnl_to_record
            self.total_trades += 1
            if pnl_to_record > 0:
                self.winning_trades += 1
            
            # Save positions
            # Save positions
            self.save_positions_to_db()
            
            self.logger.info(f"✅ Multi-leg position closed: {position.position_id}")
            self.logger.info(f"   P&L: ${pnl_to_record:.2f}")
            
        except Exception as e:
            self.logger.error(f"Error executing multi-leg exit: {e}")

    def unwind_executed_legs(self, executed_legs: List[PositionLeg]):
        """
        Unwind executed legs with escalating slippage to ensure no stranded positions.
        
        Escalation: high urgency -> 500bps -> 1000bps -> 2000bps -> direct close
        """
        import time
        
        escalation_levels = [
            {'urgency': 'high', 'slippage_bps': None, 'delay': 0.5},
            {'urgency': 'high', 'slippage_bps': 500, 'delay': 1.0},
            {'urgency': 'high', 'slippage_bps': 1000, 'delay': 1.5},
            {'urgency': 'high', 'slippage_bps': 2000, 'delay': 2.0},
        ]
        
        for leg in executed_legs:
            unwind_side = 'sell' if leg.side == 'long' else 'buy'
            unwound = False
            
            for level_idx, level in enumerate(escalation_levels):
                try:
                    self.logger.info(f"Unwind attempt {level_idx + 1}/{len(escalation_levels)} for {leg.symbol}")
                    
                    result = self.market_api.execute_order(
                        symbol=leg.symbol,
                        side=unwind_side,
                        size=leg.size,
                        reduce_only=leg.market_type == 'perp',
                        urgency=level['urgency'],
                        market_type=leg.market_type,
                        max_slippage_bps=level['slippage_bps'],
                    )
                    
                    if result and result.get('filled_size', 0) > 0:
                        self.logger.info(f"✅ Unwound leg {leg.symbol}")
                        unwound = True
                        break
                except Exception as e:
                    self.logger.warning(f"Unwind attempt {level_idx + 1} failed: {e}")
                
                if level_idx < len(escalation_levels) - 1:
                    time.sleep(level['delay'])
            
            if not unwound:
                # Final fallback: try direct API close if exchange supports it
                self.logger.critical(
                    f"❌ CRITICAL: All unwind attempts failed for {leg.symbol}. "
                    f"Attempting direct exchange position close..."
                )
                try:
                    # Try market close via SDK directly
                    if hasattr(self.market_api, 'exchange') and self.market_api.exchange:
                        # Fallback to direct SDK usage with 50% slippage tolerance
                        close_result = self.market_api.exchange.market_close(
                            leg.symbol, 
                            sz=leg.size, 
                            px=None, 
                            slippage=0.5 
                        )
                        
                        if close_result and close_result.get('status') == 'ok':
                            self.logger.info(f"✅ Direct exchange close succeeded for {leg.symbol}")
                            unwound = True
                except Exception as e:
                    self.logger.critical(f"Direct exchange close also failed for {leg.symbol}: {e}")

            if not unwound:
                self.logger.critical(f"❌ STRANDED POSITION: {leg.symbol} {leg.side} {leg.size}")

    def get_leg_price(self, symbol: str, market_type: str) -> Optional[float]:
        """Get price for a leg."""
        if market_type in ('perp', 'hip3'):
            return self.market_api.get_current_price(symbol)
        elif market_type == 'spot':
            base = symbol.split('/')[0] if '/' in symbol else symbol
            spot_token = self.market_api.get_spot_token_for_perp(base)
            return self.market_api.get_spot_price(spot_token, 'USDC') if spot_token else None
        return None

    def _get_multi_leg_position_for_symbol(self, symbol: str) -> Optional[MultiLegPosition]:
        for position in self.multi_leg_positions.values():
            if position.primary_symbol == symbol:
                return position
        return None

    def get_multi_leg_position_by_leg_symbol(self, leg_symbol: str) -> Optional[MultiLegPosition]:
        """Find a multi-leg position that contains the specified symbol as a leg."""
        for position in self.multi_leg_positions.values():
            for leg in position.legs:
                if leg.symbol == leg_symbol:
                    return position
        return None
        
    def calculate_market_volatility(self, ohlcv: Dict[str, pd.DataFrame]) -> float:
        """Calculate market volatility."""
        # Use available timeframe, prefer '15m' or '1h'
        for tf in ['15m', '1h', '4h', '1d']:
            if tf in ohlcv and len(ohlcv[tf]) >= 20:
                df = ohlcv[tf]
                break
        else:
            # Fallback to any
            if ohlcv:
                df = next(iter(ohlcv.values()))
            else:
                return 0.5
                
    def calculate_market_volatility(self, ohlcv: Dict[str, pd.DataFrame]) -> float:
        """
        Calculate annualized market volatility.
        
        Args:
            ohlcv: OHLCV data
            
        Returns:
            Annualized volatility (decimal, e.g. 0.40)
        """
        # Find best timeframe
        for tf in ['15m', '1h', '4h', '1d']:
            if tf in ohlcv and len(ohlcv[tf]) > 20:
                return calculate_annualized_volatility(ohlcv[tf]['close'])
                
        # Fallback to any timeframe
        if ohlcv:
            df = next(iter(ohlcv.values()))
            if len(df) > 2:
                return calculate_annualized_volatility(df['close'])
                
        return 0.40  # Default to target vol if no data

    def save_positions_to_db(self):
        """Save current positions to the database."""
        try:
            db = self.performance_tracker.db
            
            # Single-leg positions
            for symbol, position in self.positions.items():
                # Generate a stable ID for single-leg positions
                # Using strategy and symbol ensures uniqueness
                position_id = f"pos_{position.strategy}_{symbol}"
                
                metadata = {
                    'capital_at_risk': position.capital_at_risk,
                    'trailing_stop_enabled': position.trailing_stop_enabled,
                    'trailing_stop_pct': getattr(position, 'trailing_stop_pct', 0.0),
                    'trailing_stop_activation_pct': getattr(position, 'trailing_stop_activation_pct', 0.0),
                    'highest_price': getattr(position, 'highest_price', None),
                    'lowest_price': getattr(position, 'lowest_price', None),
                    'trailing_stop_active': getattr(position, 'trailing_stop_active', False),
                    'leverage': position.leverage
                }
                
                position_data = {
                    'position_id': position_id,
                    'strategy': position.strategy,
                    'symbol': position.symbol,
                    'side': position.side,
                    'size': position.size,
                    'leverage': position.leverage,
                    'entry_price': position.entry_price,
                    'entry_time': position.entry_time.isoformat(),
                    'stop_loss': position.stop_loss,
                    'take_profit': position.take_profit,
                    'order_id': getattr(position, 'order_id', None),  # Exchange OID
                    'metadata': metadata,
                    'legs': [] # Single leg has no sub-legs
                }
                
                db.save_position(position_data)
        
            # Multi-leg positions
            for position_id, position in self.multi_leg_positions.items():
                if hasattr(position, 'to_dict'):
                    position_data = position.to_dict()
                    # Ensure explicitly required fields map correctly if names differ
                    # DB expects 'metadata' which is usually present
                    db.save_position(position_data)
                else:
                    self.logger.error(f"MultiLegPosition {position_id} missing to_dict method")

            # self.logger.debug("Saved positions to database")
            
        except Exception as e:
            self.logger.error(f"Failed to save positions to DB: {e}")

    def load_positions_from_db(self):
        """
        Load positions from database.
        Includes backward compatibility migration from positions.json.
        """
        try:
            db = self.performance_tracker.db
            active_positions = db.get_all_active_positions()
            
            if not active_positions and os.path.exists('positions.json'):
                self.logger.info("Found positions.json but DB is empty. Migrating...")
                self._load_positions_from_json_legacy()
                self.save_positions_to_db()
                os.rename('positions.json', 'positions.json.bak')
                self.logger.info("Migration complete. positions.json renamed to .bak")
                return

            entry_time_now = datetime.now()

            for pos_data in active_positions:
                try:
                    # Restore Datetime objects
                    entry_time_str = pos_data.get('entry_time')
                    if isinstance(entry_time_str, str):
                        try:
                            entry_time = datetime.fromisoformat(entry_time_str)
                        except ValueError:
                            entry_time = entry_time_now
                    else:
                        entry_time = entry_time_now

                    legs = pos_data.get('legs', [])
                    metadata = pos_data.get('metadata', {}) or {}

                    if legs:
                        # Multi-Leg Position
                        ml_legs = []
                        for ld in legs:
                            ml_legs.append(PositionLeg(
                                symbol=ld['symbol'],
                                market_type=ld['market_type'],
                                side=ld['side'],
                                size=ld['size'],
                                entry_price=ld['entry_price'],
                                order_id=ld.get('order_id')
                            ))
                        
                        ml_pos = MultiLegPosition(
                            position_id=pos_data['position_id'],
                            strategy=pos_data['strategy'],
                            entry_time=entry_time,
                            legs=ml_legs,
                            capital_at_risk=metadata.get('capital_at_risk') if isinstance(metadata, dict) else None,
                            metadata=metadata
                        )
                        self.multi_leg_positions[pos_data['position_id']] = ml_pos
                    
                    else:
                        # Single-Leg Position
                        # Reconstruct from DB format + metadata
                        symbol = pos_data['symbol']
                        
                        position = Position(
                            symbol=symbol,
                            side=pos_data['side'],
                            size=pos_data['size'],
                            entry_price=pos_data['entry_price'],
                            entry_time=entry_time,
                            strategy=pos_data['strategy'],
                            stop_loss=pos_data.get('stop_loss'),
                            take_profit=pos_data.get('take_profit'),
                            capital_at_risk=metadata.get('capital_at_risk', 0.0),
                            leverage=pos_data.get('leverage'),
                            order_id=pos_data.get('order_id'),  # Exchange OID
                            
                            # Trailing stop params from metadata
                            trailing_stop_enabled=metadata.get('trailing_stop_enabled', False),
                            trailing_stop_pct=metadata.get('trailing_stop_pct', 0.0),
                            trailing_stop_activation_pct=metadata.get('trailing_stop_activation_pct', 0.0),
                            highest_price=metadata.get('highest_price'),
                            lowest_price=metadata.get('lowest_price'),
                            trailing_stop_active=metadata.get('trailing_stop_active', False),
                        )
                        self.positions[symbol] = position

                except Exception as e:
                    self.logger.error(f"Error loading position {pos_data.get('position_id')}: {e}")

            self.logger.info(f"Loaded {len(self.positions)} single-leg and {len(self.multi_leg_positions)} multi-leg positions from DB")
            
        except Exception as e:
            self.logger.error(f"Failed to load positions from DB: {e}")

    def _load_positions_from_json_legacy(self):
        """Legacy loader for migration purposes only."""
        try:
            with open('positions.json', 'r') as f:
                content = f.read().strip()
                if not content: return
                all_data = json.loads(content)
            
            if 'single_leg' in all_data:
                positions_data = all_data['single_leg']
                multi_leg_data = all_data.get('multi_leg', {})
            else:
                positions_data = all_data
                multi_leg_data = {}
            
            # Load single-leg
            for symbol, pos_data in positions_data.items():
                # (Simplified loading logic just for migration state population)
                try:
                    entry_time = datetime.now()
                    if pos_data.get('entry_time'):
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
                        capital_at_risk=pos_data.get('capital_at_risk', 0.0),
                        trailing_stop_enabled=pos_data.get('trailing_stop_enabled', False),
                        trailing_stop_pct=pos_data.get('trailing_stop_pct', 0.0),
                        trailing_stop_activation_pct=pos_data.get('trailing_stop_activation_pct', 0.0),
                        highest_price=pos_data.get('highest_price'),
                        lowest_price=pos_data.get('lowest_price'),
                        trailing_stop_active=pos_data.get('trailing_stop_active', False),
                        leverage=pos_data.get('leverage') 
                    )
                    self.positions[symbol] = position
                except: pass

            # Load multi-leg
            for pid, pdata in multi_leg_data.items():
                try:
                    legs = []
                    for ld in pdata.get('legs', []):
                        legs.append(PositionLeg(
                            symbol=ld['symbol'],
                            market_type=ld['market_type'],
                            side=ld['side'],
                            size=ld['size'],
                            entry_price=ld['entry_price'],
                            order_id=ld.get('order_id')
                        ))
                    
                    entry_time = datetime.now()
                    if pdata.get('entry_time'):
                        entry_time = datetime.fromisoformat(pdata['entry_time'])

                    ml_pos = MultiLegPosition(
                        position_id=pdata.get('position_id', pid),
                        strategy=pdata.get('strategy', 'unknown'),
                        entry_time=entry_time,
                        legs=legs,
                        capital_at_risk=pdata.get('capital_at_risk'),
                        metadata=pdata.get('metadata', {})
                    )
                    self.multi_leg_positions[pid] = ml_pos
                except: pass
        except Exception: pass

    def get_leg_price(self, symbol: str, market_type: str) -> Optional[float]:
        """Get current price for a leg based on market type."""
        try:
            if market_type == 'perp' or market_type == 'hip3':
                return self.market_api.get_current_price(symbol)
            elif market_type == 'spot':
                # For spot, extract base token from symbol (e.g., "BTC/USDC" -> "BTC")
                base_token = symbol.split('/')[0] if '/' in symbol else symbol
                
                # Get the spot token name from mapping (e.g., "BTC" -> "UBTC")
                spot_token = self.market_api.get_spot_token_for_perp(base_token)
                if spot_token:
                    return self.market_api.get_spot_price(spot_token, 'USDC')
                else:
                    self.logger.warning(f"No spot token mapping for {base_token}")
                    return None
            else:
                self.logger.error(f"Unknown market type: {market_type}")
                return None
        except Exception as e:
            self.logger.error(f"Error getting price for {symbol} ({market_type}): {e}")
            return None

    def estimate_funding_arb_realized_pnl(self, position: MultiLegPosition, exit_time: datetime, strategies_map: Dict[str, Any]) -> float:
        """
        Estimate realized PnL for funding-rate arbitrage as funding payments only.
        """
        try:
            perp_leg = position.get_leg("perp")
            if not perp_leg:
                return 0.0

            perp_side = (position.metadata or {}).get("perp_side", perp_leg.side)
            # side_sign: long=+1, short=-1
            side_sign = 1.0 if perp_side == "long" else -1.0

            # Approximate perp notional for funding calculation.
            notional = float(perp_leg.entry_price) * float(perp_leg.size)
            if notional <= 0:
                return 0.0

            start_time = position.entry_time
            end_time = exit_time
            if end_time <= start_time:
                return 0.0

            # Use cached funding rates from the strategy
            series = []
            strat = strategies_map.get(position.strategy)
            if strat is not None and hasattr(strat, "funding_rate_cache"):
                cache = getattr(strat, "funding_rate_cache", {}) or {}
                raw = list(cache.get(position.primary_symbol, []))
                series = [(ts, float(rate)) for ts, rate in raw if ts and rate is not None]
                series.sort(key=lambda x: x[0])

            # Fallback if we have no samples
            if not series:
                rate = None
                try:
                    rate = self.market_api.get_funding_rate(position.primary_symbol)
                except Exception:
                    rate = None
                if rate is None:
                    rate = (position.metadata or {}).get("entry_funding_rate", 0.0) or 0.0

                hours = (end_time - start_time).total_seconds() / 3600.0
                return float(-float(rate) * notional * side_sign * hours)

            # Build stepwise integral over [start_time, end_time]
            pnl_rate_integral = 0.0  # sum(rate * dt_hours)
            last_rate = series[0][1]
            for ts, rate in series:
                if ts <= start_time:
                    last_rate = rate
                else:
                    break

            last_ts = start_time
            for ts, rate in series:
                if ts <= start_time:
                    continue
                if ts >= end_time:
                    break
                dt_hours = (ts - last_ts).total_seconds() / 3600.0
                if dt_hours > 0:
                    pnl_rate_integral += last_rate * dt_hours
                last_ts = ts
                last_rate = rate

            dt_hours = (end_time - last_ts).total_seconds() / 3600.0
            if dt_hours > 0:
                pnl_rate_integral += last_rate * dt_hours

            return float(-pnl_rate_integral * notional * side_sign)
        except Exception as e:
            self.logger.debug(f"Failed to estimate funding arb pnl: {e}")
            return 0.0

    def check_liquidation_risks(self, strategies_map: Dict[str, Any] = None):
        """Check all multi-leg positions for liquidation risk."""
        if not self.multi_leg_positions:
            return
        
        liquidation_threshold = self.config['risk_management'].get('liquidation_risk_threshold', 80)
        distance_threshold = 100 - liquidation_threshold
        
        for position_id, position in list(self.multi_leg_positions.items()):
            try:
                for leg in position.legs:
                    if leg.market_type in ('perp', 'hip3'):
                        risk_info = self.market_api.check_liquidation_risk(
                            leg.symbol, 
                            threshold_pct=distance_threshold
                        )
                        
                        if risk_info.get('at_risk'):
                            self.handle_liquidation_risk(position, leg, risk_info)
                            
            except Exception as e:
                self.logger.error(f"Error checking liquidation risk for {position_id}: {e}")

    def handle_liquidation_risk(
        self, 
        position: MultiLegPosition, 
        at_risk_leg: PositionLeg,
        risk_info: Dict[str, Any]
    ):
        """Handle a delta-neutral position at liquidation risk."""
        symbol = at_risk_leg.symbol
        distance_pct = risk_info.get('distance_to_liquidation_pct', 0)
        margin_info = risk_info.get('margin_info', {})
        current_price = risk_info.get('current_price', at_risk_leg.entry_price)
        
        self.logger.warning(f"⚠️ LIQUIDATION RISK: {symbol} is {distance_pct:.1f}% from liquidation!")
        
        target_distance_pct = 30.0
        if distance_pct >= target_distance_pct:
            return
        
        position_value = abs(at_risk_leg.size * current_price)
        current_margin = margin_info.get('margin_used', 0)
        
        margin_to_add = max(current_margin * 0.5, position_value * 0.1)
        
        # Strategy 1: From perp withdrawable
        perp_balance = self.market_api.get_perp_balance()
        withdrawable = perp_balance.get('withdrawable', 0)
        
        if withdrawable >= margin_to_add:
            if self.market_api.add_position_margin(symbol, margin_to_add):
                return
        elif withdrawable > 0:
            if self.market_api.add_position_margin(symbol, withdrawable):
                margin_to_add -= withdrawable
        
        # Strategy 2: From spot
        spot_usdc = self.market_api.get_spot_balance('USDC')
        if spot_usdc >= margin_to_add:
            if self.market_api.transfer_usd_to_perp(margin_to_add):
                if self.market_api.add_position_margin(symbol, margin_to_add):
                    return
        
        # Strategy 3: Sell spot
        if margin_to_add > 0:
            spot_leg = None
            for leg in position.legs:
                if leg.market_type == 'spot':
                    spot_leg = leg
                    break
            
            if not spot_leg:
                return
            
            spot_price = self.get_leg_price(spot_leg.symbol, 'spot')
            if not spot_price or spot_price <= 0:
                return
            
            amount_to_sell = (margin_to_add * 1.05) / spot_price
            max_sell = spot_leg.size * 0.8
            amount_to_sell = min(amount_to_sell, max_sell)
            
            if amount_to_sell <= 0:
                return
            
            result = self.market_api.execute_order(
                symbol=spot_leg.symbol,
                side='sell',
                size=amount_to_sell,
                reduce_only=False,
                urgency="high",
                market_type='spot',
            )
            
            if result and result.get('filled_size', 0) > 0:
                filled_size = result['filled_size']
                spot_leg.size -= filled_size
                
                # Reduce perp leg proportionally
                perp_leg = at_risk_leg
                perp_reduction = filled_size * (spot_price / current_price)
                
                perp_result = self.market_api.execute_order(
                    symbol=perp_leg.symbol,
                    side='buy' if perp_leg.side == 'short' else 'sell',
                    size=perp_reduction,
                    reduce_only=True,
                    urgency="high",
                    market_type=perp_leg.market_type,
                )
                
                if perp_result:
                    perp_leg.size -= perp_result.get('filled_size', 0)
                
                # Transfer proceeds
                usdc_raised = filled_size * spot_price
                transfer_amount = min(usdc_raised * 0.95, margin_to_add)
                
                if self.market_api.transfer_usd_to_perp(transfer_amount):
                    self.market_api.add_position_margin(symbol, transfer_amount)
                
                self.save_positions_to_db()
